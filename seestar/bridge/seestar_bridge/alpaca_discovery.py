"""Find the seestar_alp Alpaca endpoint when ``alpaca_host`` is blank.

Two candidate sources, merged:

1. **ASCOM Alpaca discovery** — broadcast ``alpacadiscovery1`` to UDP 32227;
   every Alpaca server answers ``{"AlpacaPort": N}`` from its own address. Works
   for any seestar_alp on the same broadcast domain, including the seestar_alp
   add-on on the Supervisor network.
2. **The Supervisor add-on list** — a started add-on whose slug ends in
   ``_seestar_alp`` (any repository), reached by its stable internal hostname
   (slug with ``_`` → ``-``) on port 5555. Covers networks where broadcast does
   not arrive, and lets a UDP hit on a container IP be replaced by the add-on's
   hostname, which survives the container being recreated with a new IP.

Nothing a candidate says about itself is trusted. A candidate qualifies only if
its Alpaca API lists at least one ``Telescope`` whose ``get_event_state`` action
returns seestar_alp's ``scheduler`` block (``"Event": "Scheduler"``) — the
capability the bridge polls every cycle. ``SupportedActions`` cannot be used:
seestar_alp returns an empty list for it. This is a compatibility check, not
authentication: exactly one qualifying endpoint is auto-selected; more than one
is an error that asks the operator to set ``alpaca_host``.
"""
from __future__ import annotations

import json
import logging
import socket
import time
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from .alpaca import Alpaca

_log = logging.getLogger(__name__)

DISCOVERY_PORT = 32227
DISCOVERY_MESSAGE = b"alpacadiscovery1"
BROADCAST_ADDRESS = "255.255.255.255"
DISCOVERY_REPLY_WAIT_SEC = 2.0

#: The seestar_alp add-on's slug, whatever repository it came from.
ADDON_SLUG_SUFFIX = "_seestar_alp"
ADDON_ALPACA_PORT = 5555
SUPERVISOR_INFO_URL = "http://supervisor/supervisor/info"

#: Per-request timeouts while qualifying a candidate: short, because an endpoint
#: that cannot answer promptly is not one we want to bind to anyway.
_QUALIFY_TIMEOUT_SEC = 5
_SUPERVISOR_TIMEOUT_SEC = 5

#: Keep looking this long at startup (the driver add-on may still be starting),
#: retrying every few seconds, before giving up with an actionable error.
FIND_TIMEOUT_SEC = 180
FIND_RETRY_SEC = 5

_TELESCOPE = "Telescope"
_EVENT_STATE_ACTION = "get_event_state"


class AmbiguousAlpacaError(ValueError):
    """More than one compatible seestar_alp answered; the operator must choose."""


@dataclass(frozen=True)
class Candidate:
    host: str
    port: int
    source: str

    @property
    def host_port(self) -> str:
        return f"{self.host}:{self.port}"


def alpaca_udp_discover(
    *,
    target: str = BROADCAST_ADDRESS,
    port: int = DISCOVERY_PORT,
    wait_sec: float = DISCOVERY_REPLY_WAIT_SEC,
) -> list[tuple[str, int]]:
    """Broadcast an Alpaca discovery probe; return unique ``(ip, AlpacaPort)``.

    Malformed replies are ignored. Never raises: a send failure (no broadcast
    route) yields an empty list.
    """
    found: list[tuple[str, int]] = []
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        sock.settimeout(0.25)
        try:
            sock.sendto(DISCOVERY_MESSAGE, (target, port))
        except OSError as exc:
            _log.debug("Alpaca discovery send to %s failed: %s", target, exc)
            return found
        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            try:
                data, (ip, _src_port) = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                alpaca_port = int(json.loads(data.decode("utf-8"))["AlpacaPort"])
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                _log.debug("ignoring malformed Alpaca discovery reply from %s", ip)
                continue
            if (ip, alpaca_port) not in found:
                found.append((ip, alpaca_port))
    return found


def supervisor_addon_hosts(
    token: str | None,
    *,
    url: str = SUPERVISOR_INFO_URL,
    opener: Callable = urllib.request.urlopen,
) -> list[str]:
    """Internal hostnames of started ``*_seestar_alp`` add-ons, or ``[]``.

    Needs the add-on's ``SUPERVISOR_TOKEN`` (``hassio_api``); without one, or on
    any error, returns ``[]`` so UDP discovery alone decides.
    """
    if not token:
        return []
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with opener(req, timeout=_SUPERVISOR_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError) as exc:
        _log.debug("Supervisor add-on lookup failed: %s", exc)
        return []
    addons = (payload.get("data") or {}).get("addons") or []
    return [
        addon["slug"].replace("_", "-")
        for addon in addons
        if isinstance(addon, dict)
        and str(addon.get("slug", "")).endswith(ADDON_SLUG_SUFFIX)
        and addon.get("state") == "started"
    ]


def merge_candidates(
    udp: Iterable[tuple[str, int]],
    addon_hosts: Iterable[str],
    *,
    resolve: Callable[[str], str] = socket.gethostbyname,
) -> list[Candidate]:
    """One candidate per endpoint, preferring an add-on hostname over its IP."""
    candidates = [Candidate(ip, port, "alpaca-discovery") for ip, port in udp]
    for host in addon_hosts:
        try:
            ip = resolve(host)
        except OSError:
            ip = None
        match = next(
            (i for i, c in enumerate(candidates)
             if c.host == ip and c.port == ADDON_ALPACA_PORT),
            None,
        )
        addon = Candidate(host, ADDON_ALPACA_PORT, "supervisor")
        if match is None:
            candidates.append(addon)
        else:
            candidates[match] = addon
    return candidates


def is_seestar_alp(host_port: str, *, alpaca_factory: Callable = Alpaca) -> bool:
    """True if ``host_port`` serves a telescope answering seestar_alp's event state."""
    base = f"http://{host_port}"
    try:
        devices = alpaca_factory(base, 0).configured_devices(timeout=_QUALIFY_TIMEOUT_SEC) or []
    except (OSError, ValueError) as exc:
        _log.debug("%s: not an Alpaca server (%s)", host_port, exc)
        return False
    for device in devices:
        if not isinstance(device, dict) or device.get("DeviceType") != _TELESCOPE:
            continue
        try:
            state = alpaca_factory(base, int(device["DeviceNumber"])).action(
                _EVENT_STATE_ACTION, {}, timeout=_QUALIFY_TIMEOUT_SEC)
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
            _log.debug("%s: telescope %s has no seestar_alp event state (%s)",
                       host_port, device.get("DeviceNumber"), exc)
            continue
        scheduler = state.get("scheduler") if isinstance(state, dict) else None
        if isinstance(scheduler, dict) and scheduler.get("Event") == "Scheduler":
            return True
    return False


def find_alpaca_endpoint(
    *,
    supervisor_token: str | None,
    discover: Callable[[], list[tuple[str, int]]] = alpaca_udp_discover,
    addon_hosts: Callable[[str | None], list[str]] = supervisor_addon_hosts,
    resolve: Callable[[str], str] = socket.gethostbyname,
    qualifies: Callable[[str], bool] = is_seestar_alp,
    timeout_sec: float = FIND_TIMEOUT_SEC,
    retry_sec: float = FIND_RETRY_SEC,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Return ``host:port`` of the one compatible seestar_alp.

    Retries until ``timeout_sec`` while nothing qualifies (the driver may still
    be starting). Raises :class:`AmbiguousAlpacaError` as soon as more than one
    qualifies, and ``TimeoutError`` if none does in time.
    """
    deadline = monotonic() + timeout_sec
    while True:
        candidates = merge_candidates(discover(), addon_hosts(supervisor_token), resolve=resolve)
        qualified = [c for c in candidates if qualifies(c.host_port)]
        if len(qualified) == 1:
            chosen = qualified[0]
            _log.info("using seestar_alp at %s (found via %s)", chosen.host_port, chosen.source)
            return chosen.host_port
        if qualified:
            raise AmbiguousAlpacaError(
                "More than one seestar_alp answered ("
                + ", ".join(c.host_port for c in qualified)
                + "); set the alpaca_host option to the one to use."
            )
        if monotonic() >= deadline:
            seen = ", ".join(c.host_port for c in candidates) or "none"
            raise TimeoutError(
                f"No seestar_alp found within {timeout_sec:.0f}s (Alpaca servers seen: "
                f"{seen}). Install and start the seestar_alp add-on, or set the "
                "alpaca_host option to your own seestar_alp (host:port)."
            )
        _log.info("no seestar_alp found yet (seen: %s); retrying in %.0fs",
                  ", ".join(c.host_port for c in candidates) or "none", retry_sec)
        sleep(retry_sec)
