"""Alpaca HTTP client for seestar_alp.

Thin transport over seestar_alp's ASCOM Alpaca interface: the non-blocking
custom ``/action`` endpoint (the same call the SSC web UI uses, so we reuse
seestar_alp's single supervised scope connection), standard telescope property
GETs, and the management ``configureddevices`` enumeration.

The ``action`` path carries the bulk of the scope's live telemetry. A busy scope
answers ``method_sync`` RPCs with a wait-timeout sentinel string instead of the
real payload; we surface that as ``TimeoutError`` so callers can back off rather
than treat it as data. seestar_alp also reports many refusals IN-BAND — HTTP 200
with a ``json_result`` body of ``{"code": -1, "result": "..."}`` — which
``action`` detects and raises as ``RuntimeError`` so callers never mistake a
refusal for success.

Phase 2 note: one instance is shared by the per-scope poll thread AND paho's
command-dispatch thread, so every request (transaction-id allocation + the
urlopen round trip) is serialized under a per-instance lock — concurrent
poll+command traffic can neither corrupt the monotonic transaction ids nor
interleave on the socket.
"""
import json
import logging
import threading
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

#: seestar_alp's in-band success code (its json_result / raw device responses
#: carry ``code: 0`` on success, non-zero — typically -1 — on refusal).
_INBAND_OK_CODE = 0


class Alpaca:
    """ASCOM Alpaca client bound to one seestar_alp base URL + device number."""

    def __init__(self, base_url, device_num):
        self._base_url = base_url.rstrip("/")
        self._device_num = device_num
        self._txn = 0
        # Serializes txn allocation + the HTTP round trip: this instance is used
        # by both the poll thread and paho's command thread.
        self._lock = threading.Lock()

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

    def _get_json(self, url, timeout):
        """Serialized GET: txn allocation + round trip under the instance lock."""
        with self._lock:
            req = urllib.request.Request(
                f"{url}?ClientID={_CLIENT_ID}&ClientTransactionID={self._next_txn()}"
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())

    def action(self, name, params=None):
        """``PUT /api/v1/telescope/{n}/action`` — unwrap ``Value``, detect failure.

        Raises ``RuntimeError`` on an Alpaca-level error OR an in-band seestar_alp
        refusal (HTTP 200 whose ``Value`` dict carries a non-zero ``code`` — the
        ``json_result`` shape, e.g. import_schedule while a scheduler is active),
        and ``TimeoutError`` when a busy scope returns the wait-timeout sentinel
        (in either the bare-string or the ``method_sync`` ``{"result": ...}``
        shape).
        """
        with self._lock:
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
        raw_value = data.get("Value")
        # In-band refusal check happens BEFORE _unwrap (which would peel the
        # ``result`` off and lose the ``code`` sibling). The timeout sentinel has
        # no ``code`` key, so this cannot mask the TimeoutError path below.
        if isinstance(raw_value, dict):
            code = raw_value.get("code")
            if isinstance(code, int) and code != _INBAND_OK_CODE:
                raise RuntimeError(
                    f"{name}: seestar_alp refused (code {code}): {raw_value.get('result')}")
        val = self._unwrap(raw_value)
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
        url = f"{self._base_url}/api/v1/telescope/{self._device_num}/{prop}"
        return self._get_json(url, timeout).get("Value")

    def is_connected(self, timeout=_HTTP_TIMEOUT_SEC):
        """``GET /api/v1/telescope/{n}/connected`` — the scope's connection state.

        Returns ``True`` only when seestar_alp reports the scope connected.
        Any error (driver down, socket hang, malformed body) is treated as
        ``False`` so the Connected binary_sensor reflects an unreachable scope
        rather than aborting the poll loop. Ported from Phase-1 ``is_connected``.
        """
        try:
            url = f"{self._base_url}/api/v1/telescope/{self._device_num}/connected"
            return bool(self._get_json(url, timeout).get("Value"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            _log.info("connected probe failed (device %s): %s", self._device_num, exc)
            return False

    def configured_devices(self, timeout=_HTTP_TIMEOUT_SEC):
        """``GET /management/v1/configureddevices`` — the ``Value`` device list.

        Each entry is ``{DeviceName, DeviceNumber, ...}``; the driver enumerates
        scopes from this.
        """
        url = f"{self._base_url}/management/v1/configureddevices"
        return self._get_json(url, timeout).get("Value")
