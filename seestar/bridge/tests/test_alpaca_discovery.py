"""Finding the seestar_alp endpoint when alpaca_host is blank.

Candidates come from ASCOM Alpaca UDP discovery and the Supervisor add-on list;
only an endpoint that answers seestar_alp's event state qualifies, and only a
single qualifying endpoint is auto-selected.
"""
import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from seestar_bridge.alpaca_discovery import (
    AmbiguousAlpacaError,
    Candidate,
    alpaca_udp_discover,
    find_alpaca_endpoint,
    is_seestar_alp,
    merge_candidates,
    supervisor_addon_hosts,
)

# --- UDP discovery ------------------------------------------------------------


def _udp_responder(replies):
    """Answer each discovery datagram with every payload in ``replies``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(3)
    seen = []

    def serve():
        try:
            data, addr = sock.recvfrom(1024)
        except OSError:
            return
        seen.append(data)
        for payload in replies:
            sock.sendto(payload, addr)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return sock, thread, seen


def test_udp_discovery_returns_unique_valid_replies():
    sock, thread, seen = _udp_responder([
        b'{"AlpacaPort": 5555}',
        b'{"AlpacaPort": 5555}',      # duplicate
        b"not json",                  # malformed
        b'{"SomethingElse": 1}',      # wrong shape
        b'{"AlpacaPort": 11111}',     # second server on the same host
    ])
    try:
        found = alpaca_udp_discover(
            target="127.0.0.1", port=sock.getsockname()[1], wait_sec=0.6)
    finally:
        thread.join(timeout=3)
        sock.close()
    assert seen == [b"alpacadiscovery1"]
    assert found == [("127.0.0.1", 5555), ("127.0.0.1", 11111)]


def test_udp_discovery_with_no_responder_is_empty():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    assert alpaca_udp_discover(target="127.0.0.1", port=port, wait_sec=0.3) == []


# --- Supervisor add-on list ---------------------------------------------------


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _opener(payload, calls):
    def opener(req, timeout):
        calls.append(req.get_header("Authorization"))
        return _Resp(json.dumps(payload).encode())
    return opener


def test_supervisor_lists_only_started_seestar_alp_addons():
    payload = {"result": "ok", "data": {"addons": [
        {"slug": "ff3a9a94_seestar_alp", "state": "started"},
        {"slug": "local_seestar_alp", "state": "stopped"},
        {"slug": "core_mosquitto", "state": "started"},
        {"slug": "ff29c31e_seestar", "state": "started"},
        {"slug": "abc_seestar_alp_extra", "state": "started"},
    ]}}
    calls = []
    hosts = supervisor_addon_hosts("tok", opener=_opener(payload, calls))
    assert hosts == ["ff3a9a94-seestar-alp"]
    assert calls == ["Bearer tok"]


def test_supervisor_lookup_needs_a_token():
    calls = []
    assert supervisor_addon_hosts(None, opener=_opener({}, calls)) == []
    assert calls == []


def test_supervisor_lookup_failure_is_empty():
    def failing(req, timeout):
        raise OSError("no route to supervisor")
    assert supervisor_addon_hosts("tok", opener=failing) == []


# --- merging ------------------------------------------------------------------


def test_addon_hostname_replaces_its_own_udp_hit():
    resolve = {"ff3a9a94-seestar-alp": "172.30.33.8"}.__getitem__
    merged = merge_candidates(
        [("172.30.33.8", 5555), ("192.168.1.50", 5555)],
        ["ff3a9a94-seestar-alp"],
        resolve=resolve,
    )
    assert merged == [
        Candidate("ff3a9a94-seestar-alp", 5555, "supervisor"),
        Candidate("192.168.1.50", 5555, "alpaca-discovery"),
    ]


def test_addon_not_heard_over_udp_is_still_a_candidate():
    def unresolvable(host):
        raise OSError("no such host")
    merged = merge_candidates([("172.30.33.9", 11111)], ["x-seestar-alp"], resolve=unresolvable)
    assert [c.host_port for c in merged] == ["172.30.33.9:11111", "x-seestar-alp:5555"]


# --- qualification against a stub Alpaca server -------------------------------


def _alpaca_server(devices, action_value=None, action_error=None):
    """Minimal Alpaca: configureddevices + telescope action."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body):
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            if self.path.startswith("/management/v1/configureddevices"):
                self._send({"Value": devices, "ErrorNumber": 0})
            else:
                self.send_error(404)

        def do_PUT(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if action_error:
                self._send({"Value": "", "ErrorNumber": 1024, "ErrorMessage": action_error})
            else:
                self._send({"Value": action_value, "ErrorNumber": 0})

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"127.0.0.1:{srv.server_address[1]}"


TELESCOPE = [{"DeviceName": "Seestar S30 Pro", "DeviceType": "Telescope", "DeviceNumber": 1}]
SEESTAR_EVENT_STATE = {
    "jsonrpc": "2.0", "code": 0, "command": "get_event_state",
    "result": {"scheduler": {"Event": "Scheduler", "state": "stopped"}},
}


@pytest.mark.parametrize(
    ("devices", "action_value", "action_error", "expected"),
    [
        (TELESCOPE, SEESTAR_EVENT_STATE, None, True),
        # Another vendor's telescope driver: the custom action is unsupported.
        (TELESCOPE, None, "Action get_event_state is not implemented", False),
        # A telescope whose action answers, but not with seestar_alp's state.
        (TELESCOPE, {"code": 0, "result": {"something": "else"}}, None, False),
        # seestar_alp-shaped state, but only on a non-telescope device.
        ([{"DeviceName": "Wheel", "DeviceType": "FilterWheel", "DeviceNumber": 0}],
         SEESTAR_EVENT_STATE, None, False),
    ],
)
def test_only_seestar_alp_event_state_qualifies(devices, action_value, action_error, expected):
    srv, host_port = _alpaca_server(devices, action_value, action_error)
    try:
        assert is_seestar_alp(host_port) is expected
    finally:
        srv.shutdown()


def test_unreachable_endpoint_does_not_qualify():
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    assert is_seestar_alp(f"127.0.0.1:{port}") is False


# --- selection ----------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _find(rounds, *, addon_hosts=(), qualifying=(), timeout_sec=30, clock=None):
    """Run find_alpaca_endpoint over scripted UDP rounds (last round repeats)."""
    clock = clock or _Clock()
    rounds = list(rounds)

    def discover():
        return rounds.pop(0) if len(rounds) > 1 else rounds[0]

    return find_alpaca_endpoint(
        supervisor_token="tok",
        discover=discover,
        addon_hosts=lambda token: list(addon_hosts),
        resolve=lambda host: "10.9.9.9",
        qualifies=lambda host_port: host_port in qualifying,
        timeout_sec=timeout_sec,
        retry_sec=5,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


def test_single_qualifying_endpoint_is_selected_ignoring_non_seestar():
    endpoint = _find(
        [[("172.30.33.8", 5555), ("192.168.1.20", 11111)]],
        qualifying={"172.30.33.8:5555"},
    )
    assert endpoint == "172.30.33.8:5555"


def test_two_qualifying_endpoints_require_an_explicit_choice():
    with pytest.raises(AmbiguousAlpacaError, match="alpaca_host") as err:
        _find(
            [[("172.30.33.8", 5555), ("192.168.1.50", 5555)]],
            qualifying={"172.30.33.8:5555", "192.168.1.50:5555"},
        )
    assert "172.30.33.8:5555" in str(err.value) and "192.168.1.50:5555" in str(err.value)


def test_waits_for_a_driver_that_is_still_starting():
    clock = _Clock()
    endpoint = _find(
        [[], [], [("172.30.33.8", 5555)]],
        qualifying={"172.30.33.8:5555"},
        clock=clock,
    )
    assert endpoint == "172.30.33.8:5555"
    assert clock.sleeps == [5, 5]


def test_supervisor_addon_found_when_udp_hears_nothing():
    endpoint = _find([[]], addon_hosts=["ff3a9a94-seestar-alp"],
                     qualifying={"ff3a9a94-seestar-alp:5555"})
    assert endpoint == "ff3a9a94-seestar-alp:5555"


def test_gives_up_with_an_actionable_error():
    with pytest.raises(TimeoutError, match="seestar_alp add-on") as err:
        _find([[("192.168.1.20", 11111)]], qualifying=set(), timeout_sec=12)
    assert "alpaca_host" in str(err.value)
    assert "192.168.1.20:11111" in str(err.value)
