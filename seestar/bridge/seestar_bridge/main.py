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

from .alpaca import Alpaca
from .discovery import discover_addresses
from .entities import slug
from .mqtt import build_client
from .scope import ScopeWorker
from .settings import Settings, load_settings

_log = logging.getLogger(__name__)

_DEVICE_NAME_KEY = "DeviceName"
_DEVICE_NUMBER_KEY = "DeviceNumber"

_MQTT_KEEPALIVE_SEC = 60
_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def assign_device_ids(devices: list[dict]) -> dict[int, str]:
    """Map each device's ``DeviceNumber`` -> a stable, unique HA device id.

    The id is the slug of the scope name (the rotating Alpaca ``UniqueID`` is
    deliberately not used). When two scopes slug to the same id, the colliding
    ones are disambiguated by appending ``_<device_num>`` so each HA device stays
    distinct and stable across restarts.
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


def build_workers(alpaca, settings: Settings, mqtt_client) -> list[ScopeWorker]:
    """Enumerate scopes, publish discovery, and return a worker per scope.

    Resolves each scope's preview HTTP base via the 3-tier discovery (best-effort;
    a scope with no resolved address simply gets ``None`` and skips its preview),
    publishes retained MQTT discovery for every scope, and constructs (but does
    not start) one :class:`ScopeWorker` each.
    """
    devices = alpaca.configured_devices() or []
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
        )
        worker.publish_discovery()
        workers.append(worker)
        _log.info("scope %s (device_num=%d) discovered; preview=%s",
                  worker.device_id, device_num, scope_http_base or "none")
    return workers


def main() -> None:
    """Compose the real pieces and run a worker thread per scope (blocks forever)."""
    logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT)
    settings = load_settings(_options(), dict(os.environ))

    # Device 0 is a placeholder for the management enumeration call; per-scope
    # Alpaca clients (bound to the real device numbers) are built in build_workers.
    alpaca = Alpaca(settings.alpaca_base, 0)

    mqtt_client = build_client(settings.mqtt)
    mqtt_client.connect(settings.mqtt.host, settings.mqtt.port, keepalive=_MQTT_KEEPALIVE_SEC)
    mqtt_client.loop_start()

    workers = build_workers(alpaca, settings, mqtt_client)
    if not workers:
        _log.warning("no scopes enumerated from Alpaca; nothing to publish")

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
