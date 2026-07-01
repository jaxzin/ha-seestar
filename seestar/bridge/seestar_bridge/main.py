"""Orchestrator: enumerate scopes, publish discovery, spawn a worker per scope.

``main`` wires the building blocks: load :class:`~seestar_bridge.settings.Settings`
from the add-on options + environment, build and connect one shared MQTT client,
enumerate the configured telescopes via Alpaca, resolve each scope's own HTTP
address (preview only) via the 3-tier discovery, publish MQTT discovery for each
as its own HA device, then run one :class:`~seestar_bridge.scope.ScopeWorker`
per scope on its own thread.

The scope→worker wiring is factored into :func:`build_workers` so it can be
exercised end-to-end (enumerate → discover → publish) against fakes without a
real broker or a running event loop.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

from .alpaca import Alpaca
from .discovery import discover_addresses
from .entities import slug
from .mqtt import COMMAND_QOS, build_client, set_router
from .scope import ScopeWorker
from .settings import Settings, load_settings, python_log_level

_log = logging.getLogger(__name__)

_DEVICE_NAME_KEY = "DeviceName"
_DEVICE_NUMBER_KEY = "DeviceNumber"

_MQTT_KEEPALIVE_SEC = 60
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

#: In bundled mode the bridge and the seestar_alp driver start concurrently under
#: s6, so the first ``configureddevices`` enumeration can hit a not-yet-bound
#: :5555 and raise ConnectionRefused. Retry the enumeration on connection errors
#: with a bounded backoff rather than crash-looping the always-on bridge. We do
#: NOT add a hard s6 bridge->seestar-alp dependency, which would couple the bridge
#: to the conditionally-downed driver.
_ALPACA_ENUMERATE_RETRY_SEC = 3
_ALPACA_ENUMERATE_TIMEOUT_SEC = 60

#: Process-level liveness topic shared by every scope. The broker publishes
#: ``offline`` here (the Last-Will) if the bridge dies, and main publishes
#: ``online`` after connecting; every entity ANDs this with its per-scope
#: availability topic, so a dead bridge marks ALL scopes unavailable at once.
BRIDGE_AVAILABILITY_TOPIC = "seestar/bridge/availability"
_PAYLOAD_AVAILABLE = "online"
_PAYLOAD_NOT_AVAILABLE = "offline"


def assign_device_ids(devices: list[dict]) -> dict[int, str]:
    """Map each device's ``DeviceNumber`` -> a stable, unique HA device id.

    The id is the slug of the scope name (the rotating Alpaca ``UniqueID`` is
    deliberately not used). When two scopes slug to the same id, the colliding
    ones are disambiguated by appending ``_<device_num>`` so each HA device stays
    distinct and stable across restarts.

    Known caveat: the disambiguation suffix is *conditional* on a same-named
    sibling being present. If two scopes share a name and one is later removed,
    the survivor's slug no longer collides, so its id loses the ``_<device_num>``
    suffix — i.e. its HA device id changes across that reconfiguration. This is
    acceptable for v1; the fix is operational: give same-named scopes distinct
    names so neither id ever carries a collision suffix.
    """
    def base_id(device: dict) -> str:
        return slug(device.get(_DEVICE_NAME_KEY, "")) or f"scope_{int(device[_DEVICE_NUMBER_KEY])}"

    counts: dict[str, int] = {}
    for device in devices:
        counts[base_id(device)] = counts.get(base_id(device), 0) + 1

    device_ids: dict[int, str] = {}
    for device in devices:
        device_num = int(device[_DEVICE_NUMBER_KEY])
        base = base_id(device)
        # Collision: make it unique + stable by pinning to the device number.
        device_ids[device_num] = f"{base}_{device_num}" if counts[base] > 1 else base
    return device_ids


def enumerate_devices(
    alpaca,
    *,
    retry_sec: float = _ALPACA_ENUMERATE_RETRY_SEC,
    timeout_sec: float = _ALPACA_ENUMERATE_TIMEOUT_SEC,
    sleep=time.sleep,
    monotonic=time.monotonic,
) -> list[dict]:
    """Enumerate Alpaca devices, retrying on connection errors within a window.

    In bundled mode the driver may not have bound :5555 yet when the bridge starts,
    so :meth:`Alpaca.configured_devices` can raise ``ConnectionRefused`` (surfaced
    as ``URLError``, an ``OSError`` subclass). Retry every ``retry_sec`` until the
    driver answers or ``timeout_sec`` elapses, logging at info instead of crashing
    on a traceback. Non-connection errors (and a timeout) propagate so genuine
    misconfiguration still fails loudly.
    """
    deadline = monotonic() + timeout_sec
    while True:
        try:
            return alpaca.configured_devices() or []
        except OSError as exc:  # URLError (connection refused) is an OSError
            if monotonic() >= deadline:
                raise TimeoutError(
                    f"Alpaca driver did not come up within {timeout_sec:.0f}s: {exc}"
                ) from exc
            _log.info("waiting for the Alpaca driver to come up... (%s)", exc)
            sleep(retry_sec)


def build_workers(alpaca, settings: Settings, mqtt_client) -> list[ScopeWorker]:
    """Enumerate scopes, publish discovery, and return a worker per scope.

    Resolves each scope's preview HTTP base via the 3-tier discovery (best-effort;
    a scope with no resolved address simply gets ``None`` and skips its preview),
    publishes retained MQTT discovery for every scope, and constructs (but does
    not start) one :class:`ScopeWorker` each.
    """
    devices = enumerate_devices(alpaca)
    device_ids = assign_device_ids(devices)
    addresses = discover_addresses(
        config_toml_path=settings.config_toml_path,
        webui_base=settings.webui_base,
        devices=devices,
    )

    workers: list[ScopeWorker] = []
    for device in devices:
        device_num = int(device[_DEVICE_NUMBER_KEY])
        ip_address = addresses.get(device_num)
        scope_http_base = f"http://{ip_address}" if ip_address else None
        worker = ScopeWorker(
            alpaca=Alpaca(settings.alpaca_base, device_num),
            device=device,
            settings=settings,
            mqtt_client=mqtt_client,
            scope_http_base=scope_http_base,
            device_id=device_ids[device_num],
            bridge_availability_topic=BRIDGE_AVAILABILITY_TOPIC,
        )
        worker.publish_discovery()
        workers.append(worker)
        _log.info("scope %s (device_num=%d) discovered; preview=%s",
                  worker.device_id, device_num, scope_http_base or "none")
    return workers


def make_command_router(workers: list[ScopeWorker]):
    """Build the ``(topic, payload)`` router that fans commands to owning workers.

    The shared MQTT client subscribes to every scope's ``cmd/#`` filter, so all
    scopes' commands arrive on one ``on_message``. The returned router hands each
    message to the SINGLE worker that owns the topic (matched by its per-scope base
    topic) and to no other — the load-bearing per-scope isolation: a command for
    device A can only ever reach device A's worker. An unowned topic (e.g. a stray
    publish) is logged and dropped rather than silently swallowed.
    """
    def route(topic: str, payload: str) -> None:
        for worker in workers:
            if worker.owns_topic(topic):
                worker.handle_command(topic, payload)
                return
        _log.warning("command on %s matched no scope; dropping", topic)

    return route


def subscribe_commands(mqtt_client, workers: list[ScopeWorker]) -> None:
    """Install the command router + subscribe the shared client to every scope.

    Registers one ``on_connect``/``on_message`` pair (via :func:`mqtt.set_router`)
    so the per-scope ``cmd/#`` filters are (re)subscribed on every connect and
    reconnect. Because the caller connects the client BEFORE the workers exist,
    ``on_connect`` has already fired for the initial link; we therefore also issue
    the subscriptions once here so the running connection starts receiving commands
    immediately, while ``on_connect`` covers all later reconnects. A run with no
    workers registers nothing (there is nothing to command).
    """
    if not workers:
        return
    filters = [worker.command_topic_filter for worker in workers]
    set_router(mqtt_client, make_command_router(workers), filters)
    for topic_filter in filters:
        mqtt_client.subscribe(topic_filter, qos=COMMAND_QOS)
        _log.info("subscribed to command topics: %s", topic_filter)


def main() -> None:
    """Compose the real pieces and run a worker thread per scope (blocks forever)."""
    settings = load_settings(_options(), dict(os.environ))
    # Honor the operator's log_level option. The HA-convention levels that have no
    # stdlib name (trace/notice/fatal) are mapped to their nearest real level by
    # python_log_level, so every config.yaml enum value takes effect end-to-end
    # instead of silently degrading to INFO.
    level = getattr(logging, python_log_level(settings.log_level), logging.INFO)
    logging.basicConfig(level=level, format=_LOG_FORMAT)

    # Device 0 is a placeholder for the management enumeration call; per-scope
    # Alpaca clients (bound to the real device numbers) are built in build_workers.
    alpaca = Alpaca(settings.alpaca_base, 0)

    mqtt_client = build_client(
        settings.mqtt,
        will_topic=BRIDGE_AVAILABILITY_TOPIC,
        will_payload=_PAYLOAD_NOT_AVAILABLE,
    )
    mqtt_client.connect(settings.mqtt.host, settings.mqtt.port, keepalive=_MQTT_KEEPALIVE_SEC)
    mqtt_client.loop_start()
    # Announce the bridge alive (retained) once connected; the LWT above flips
    # this back to 'offline' if the process dies without a clean disconnect.
    mqtt_client.publish(BRIDGE_AVAILABILITY_TOPIC, _PAYLOAD_AVAILABLE, retain=True)

    workers = build_workers(alpaca, settings, mqtt_client)
    if not workers:
        _log.warning("no scopes enumerated from Alpaca; nothing to publish")

    # Wire the command (subscribe) path: route each inbound command to its owning
    # worker, and install the shared client's on_connect so every scope's cmd/#
    # filter is (re)subscribed on connect AND reconnect. The initial CONNECT above
    # already fired before the workers existed, so subscribe once here explicitly;
    # the on_connect handler then covers every subsequent reconnect.
    subscribe_commands(mqtt_client, workers)

    threads = []
    for worker in workers:
        thread = threading.Thread(target=worker.run, name=f"scope-{worker.device_id}", daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join()


def _options() -> dict:
    """Load the add-on options JSON Supervisor mounts at /data/options.json.

    Returns an empty dict if the file is absent (e.g. local/dev runs), letting
    ``load_settings`` raise its actionable error when nothing is configured.
    """
    options_path = os.environ.get("ADDON_OPTIONS_PATH", "/data/options.json")
    try:
        with open(options_path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


if __name__ == "__main__":
    main()
