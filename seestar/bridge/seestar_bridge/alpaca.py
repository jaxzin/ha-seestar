"""Alpaca HTTP client for seestar_alp.

Thin transport over seestar_alp's ASCOM Alpaca interface: the non-blocking
custom ``/action`` endpoint (the same call the SSC web UI uses, so we reuse
seestar_alp's single supervised scope connection), standard telescope property
GETs, and the management ``configureddevices`` enumeration.

The ``action`` path carries the bulk of the scope's live telemetry. A busy scope
answers ``method_sync`` RPCs with a wait-timeout sentinel string instead of the
real payload; we surface that as ``TimeoutError`` so callers can back off rather
than treat it as data.
"""
import json
import logging
import urllib.error
import urllib.request

_log = logging.getLogger(__name__)

#: ASCOM requires ClientID / ClientTransactionID on every request. We use a
#: single logical client and a monotonically increasing transaction id.
_CLIENT_ID = 1

#: Default per-request timeout (seconds). The action endpoint is non-blocking on
#: the scope side, but the socket read can still hang if seestar_alp is wedged.
_HTTP_TIMEOUT_SEC = 15

#: A busy scope returns this substring (as the method_sync ``result``) instead of
#: the real payload; we translate it into ``TimeoutError``.
_TIMEOUT_SENTINEL = "Exceeded allotted wait time"


class Alpaca:
    """ASCOM Alpaca client bound to one seestar_alp base URL + device number."""

    def __init__(self, base_url, device_num):
        self._base_url = base_url.rstrip("/")
        self._device_num = device_num
        self._txn = 0

    def _next_txn(self):
        self._txn += 1
        return self._txn

    @staticmethod
    def _unwrap(value):
        """Peel seestar_alp's nesting off an Alpaca ``Value``.

        ``method_sync`` wraps its payload as ``{"method": ..., "result": ...}``;
        unwrap that when the result is itself structured.
        """
        if isinstance(value, dict) and "result" in value and isinstance(value["result"], (dict, list)):
            value = value["result"]
        return value

    def action(self, name, params=None):
        """``PUT /api/v1/telescope/{n}/action`` — unwrap ``Value``, detect timeout.

        Raises ``RuntimeError`` on an Alpaca-level error and ``TimeoutError`` when
        a busy scope returns the wait-timeout sentinel (in either the bare-string
        or the ``method_sync`` ``{"result": ...}`` shape).
        """
        body = json.dumps({
            "Action": name,
            "Parameters": json.dumps(params or {}),
            "ClientID": _CLIENT_ID,
            "ClientTransactionID": self._next_txn(),
        }).encode()
        req = urllib.request.Request(
            f"{self._base_url}/api/v1/telescope/{self._device_num}/action",
            data=body, method="PUT", headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_SEC) as resp:
            data = json.loads(resp.read().decode())
        if data.get("ErrorNumber"):
            raise RuntimeError(f"{name}: Alpaca error {data['ErrorNumber']} {data.get('ErrorMessage')}")
        val = self._unwrap(data.get("Value"))
        # The wait-timeout sentinel is a plain string that _unwrap leaves wrapped
        # (it only unwraps dict/list results). Detect it in either shape.
        sentinel = val.get("result") if isinstance(val, dict) else val
        if isinstance(sentinel, str) and _TIMEOUT_SENTINEL in sentinel:
            raise TimeoutError(f"{name} timed out (scope busy)")
        return val

    def get(self, prop, timeout=_HTTP_TIMEOUT_SEC):
        """``GET /api/v1/telescope/{n}/{prop}`` — return the ``Value``.

        Used for standard ASCOM telescope properties (e.g. ``atpark``,
        ``sitelatitude``).
        """
        req = urllib.request.Request(
            f"{self._base_url}/api/v1/telescope/{self._device_num}/{prop}"
            f"?ClientID={_CLIENT_ID}&ClientTransactionID={self._next_txn()}"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()).get("Value")

    def is_connected(self, timeout=_HTTP_TIMEOUT_SEC):
        """``GET /api/v1/telescope/{n}/connected`` — the scope's connection state.

        Returns ``True`` only when seestar_alp reports the scope connected.
        Any error (driver down, socket hang, malformed body) is treated as
        ``False`` so the Connected binary_sensor reflects an unreachable scope
        rather than aborting the poll loop. Ported from Phase-1 ``is_connected``.
        """
        try:
            req = urllib.request.Request(
                f"{self._base_url}/api/v1/telescope/{self._device_num}/connected"
                f"?ClientID={_CLIENT_ID}&ClientTransactionID={self._next_txn()}"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return bool(json.loads(resp.read().decode()).get("Value"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log.info("connected probe failed (device %s): %s", self._device_num, exc)
            return False

    def configured_devices(self, timeout=_HTTP_TIMEOUT_SEC):
        """``GET /management/v1/configureddevices`` — the ``Value`` device list.

        Each entry is ``{DeviceName, DeviceNumber, ...}``; the driver enumerates
        scopes from this.
        """
        req = urllib.request.Request(
            f"{self._base_url}/management/v1/configureddevices"
            f"?ClientID={_CLIENT_ID}&ClientTransactionID={self._next_txn()}"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode()).get("Value")
