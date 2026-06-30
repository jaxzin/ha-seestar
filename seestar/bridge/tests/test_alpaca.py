import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from seestar_bridge.alpaca import Alpaca


def _serve(routes):
    class H(BaseHTTPRequestHandler):
        def _send(self, obj):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(obj).encode())

        def do_GET(self):
            self._send(routes[self.path.split("?")[0]])

        def do_PUT(self):
            # Drain the request body before responding; otherwise the client may
            # still be writing it when we close the socket, which surfaces as a
            # ConnectionResetError on the client side.
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            self._send(routes["PUT"])

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_action_unwraps_and_detects_timeout():
    srv, base = _serve({"PUT": {"Value": {"method": "get_device_state", "result": "Error: Exceeded allotted wait time"}}})
    with pytest.raises(TimeoutError):
        Alpaca(base, 1).action("method_sync", {"method": "get_device_state"})
    srv.shutdown()


def test_action_bare_string_timeout_sentinel_raises():
    # Sentinel arrives as a bare string Value (no method/result wrapper); this
    # pins the ``else val`` branch of the dual-shape sentinel detection.
    srv, base = _serve({"PUT": {"Value": "Error: Exceeded allotted wait time"}})
    with pytest.raises(TimeoutError):
        Alpaca(base, 1).action("method_sync", {"method": "get_device_state"})
    srv.shutdown()


def test_action_alpaca_error_raises_runtime_error():
    srv, base = _serve({"PUT": {"Value": None, "ErrorNumber": 1, "ErrorMessage": "boom"}})
    with pytest.raises(RuntimeError):
        Alpaca(base, 1).action("method_sync", {"method": "get_device_state"})
    srv.shutdown()


def test_action_happy_path_returns_unwrapped_result():
    srv, base = _serve({"PUT": {"Value": {"method": "m", "result": {"k": "v"}}}})
    assert Alpaca(base, 1).action("method_sync", {"method": "m"}) == {"k": "v"}
    srv.shutdown()


def test_get_returns_value():
    srv, base = _serve({"/api/v1/telescope/1/atpark": {"Value": True}})
    assert Alpaca(base, 1).get("atpark") is True
    srv.shutdown()


def test_configured_devices_returns_list():
    srv, base = _serve({"/management/v1/configureddevices": {"Value": [{"DeviceName": "Seestar S30 Pro", "DeviceNumber": 1}]}})
    assert Alpaca(base, 1).configured_devices()[0]["DeviceNumber"] == 1
    srv.shutdown()
