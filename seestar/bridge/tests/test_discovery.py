"""Tests for the 3-tier scope-address discovery.

discover_addresses resolves {device_num: ip_address} for the preview only,
trying config.toml (bundled/local) -> {webui}/config.json -> {webui}/config
HTML scrape, in that priority. Any source failure falls through; it never
raises. Missing entries are simply absent (the preview is skipped for them).
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from seestar_bridge.discovery import (
    discover_addresses,
    parse_config_html,
    parse_config_json,
    parse_config_toml,
)

# --- fixtures -----------------------------------------------------------------

CONFIG_TOML = """
[[seestars]]
name = "Seestar Alpha"
ip_address = "10.0.0.11"
device_num = 1

[[seestars]]
name = "Seestar Beta"
ip_address = "10.0.0.12"
device_num = 2
"""

CONFIG_JSON = {
    "devices": [
        {"device_num": 1, "ip_address": "10.0.0.21"},
        {"device_num": 2, "ip_address": "10.0.0.22"},
    ]
}

CONFIG_HTML = """
<html><body><form>
  <input type="text" name="ss_name" value="Seestar Alpha">
  <input type="text" name="ss_ip_address" value="10.0.0.31">
  <input type="text" name="ss_name" value="Seestar Beta">
  <input type="text" name="ss_ip_address" value="10.0.0.32">
</form></body></html>
"""

DEVICES = [
    {"DeviceName": "Seestar Alpha", "DeviceNumber": 1},
    {"DeviceName": "Seestar Beta", "DeviceNumber": 2},
]


def _serve(routes, html_404=False):
    """Stub HTTP server. routes maps a path -> JSON-serializable object.

    If html_404 is set, /config.json responds 404 so the HTML fallback engages.
    """

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/config.json" and html_404:
                self.send_response(404)
                self.end_headers()
                return
            if path == "/config":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(routes[path].encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(routes[path]).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# --- parser unit tests --------------------------------------------------------


def test_parse_config_toml_maps_device_num_to_ip():
    assert parse_config_toml(CONFIG_TOML) == {1: "10.0.0.11", 2: "10.0.0.12"}


def test_parse_config_json_maps_device_num_to_ip():
    assert parse_config_json(CONFIG_JSON) == {1: "10.0.0.21", 2: "10.0.0.22"}


def test_parse_config_html_pairs_name_to_ip_in_document_order():
    assert parse_config_html(CONFIG_HTML) == {
        "Seestar Alpha": "10.0.0.31",
        "Seestar Beta": "10.0.0.32",
    }


# --- priority / fallback ------------------------------------------------------


def test_toml_present_is_used(tmp_path):
    toml_file = tmp_path / "config.toml"
    toml_file.write_text(CONFIG_TOML)
    # webui still up with different addresses; toml must win.
    srv, base = _serve({"/config.json": CONFIG_JSON})
    try:
        result = discover_addresses(
            config_toml_path=str(toml_file), webui_base=base, devices=DEVICES
        )
    finally:
        srv.shutdown()
    assert result == {1: "10.0.0.11", 2: "10.0.0.12"}


def test_toml_absent_then_json_200(tmp_path):
    srv, base = _serve({"/config.json": CONFIG_JSON})
    try:
        result = discover_addresses(
            config_toml_path=str(tmp_path / "missing.toml"),
            webui_base=base,
            devices=DEVICES,
        )
    finally:
        srv.shutdown()
    assert result == {1: "10.0.0.21", 2: "10.0.0.22"}


def test_json_404_falls_back_to_html_scrape(tmp_path):
    srv, base = _serve({"/config": CONFIG_HTML}, html_404=True)
    try:
        result = discover_addresses(
            config_toml_path=str(tmp_path / "missing.toml"),
            webui_base=base,
            devices=DEVICES,
        )
    finally:
        srv.shutdown()
    # HTML keys ss_name -> matched back to DeviceName -> DeviceNumber.
    assert result == {1: "10.0.0.31", 2: "10.0.0.32"}


def test_nothing_resolvable_returns_empty(tmp_path):
    # No toml, no webui at all -> empty, no raise.
    result = discover_addresses(
        config_toml_path=str(tmp_path / "missing.toml"),
        webui_base="http://127.0.0.1:1",  # nothing listening
        devices=DEVICES,
    )
    assert result == {}


def test_no_webui_base_and_no_toml_returns_empty(tmp_path):
    result = discover_addresses(
        config_toml_path=None, webui_base=None, devices=DEVICES
    )
    assert result == {}


def test_html_only_includes_matched_devices(tmp_path):
    # HTML advertises a scope name not in the device list; it is dropped.
    html = (
        '<input name="ss_name" value="Seestar Alpha">'
        '<input name="ss_ip_address" value="10.0.0.31">'
        '<input name="ss_name" value="Unknown Scope">'
        '<input name="ss_ip_address" value="10.0.0.99">'
    )
    srv, base = _serve({"/config": html}, html_404=True)
    try:
        result = discover_addresses(
            config_toml_path=str(tmp_path / "missing.toml"),
            webui_base=base,
            devices=DEVICES,
        )
    finally:
        srv.shutdown()
    assert result == {1: "10.0.0.31"}
