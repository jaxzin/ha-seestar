"""The Supervisor WS helper must never print secrets from add-on info replies."""
import importlib.util
from pathlib import Path

_PATH = Path(__file__).resolve().parents[3] / "scripts" / "ha-supervisor-api.py"
_spec = importlib.util.spec_from_file_location("ha_supervisor_api", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_info_reply_secrets_are_masked_but_plain_options_survive():
    reply = {"success": True, "result": {"version": "1.0.7", "options": {
        "mqtt_password": "hunter2", "interop_pem": "-----BEGIN-----",
        "mqtt_username": "seestar", "alpaca_host": "", "mqtt_ssl": False,
        "scopes": [{"name": "S30", "host": "192.168.10.50"}]}}}
    out = _mod._redact(reply)["result"]
    assert out["options"]["mqtt_password"] == "<redacted>"
    assert out["options"]["interop_pem"] == "<redacted>"
    assert out["options"]["mqtt_username"] == "seestar"
    assert out["options"]["scopes"] == [{"name": "S30", "host": "192.168.10.50"}]
    assert "hunter2" not in str(out)
