"""Smoke test for the orchestrator wiring (enumerate -> discover -> publish).

Points the bridge at an ``http.server`` stub serving the Alpaca management
``configureddevices`` enumeration plus a canned ``get_event_state`` for the
action endpoint, with a fake MQTT client capturing every publish. Asserts the
full path: each enumerated device gets its own namespaced discovery configs and
at least one state publish — not just the pure ``build_state``.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from seestar_bridge.entities import ENTITIES
from seestar_bridge.main import assign_device_ids, build_workers
from seestar_bridge.settings import MqttSettings, Settings

DEVICES = [
    {"DeviceName": "Seestar Alpha", "DeviceNumber": 1},
    {"DeviceName": "Seestar Beta", "DeviceNumber": 2},
]

EVENT_STATE = {
    "View": {"target_name": "NGC 7000", "state": "working"},
    "Stack": {"state": "working", "stacked_frame": 5},
    "AutoGoto": {"state": "complete"},
}


def _serve():
    """Stub seestar_alp: configureddevices + a canned get_event_state action."""

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/management/v1/configureddevices":
                self._send_json({"Value": DEVICES})
                return
            # Standard property GET (e.g. sitelatitude/sitelongitude): unset GPS.
            self._send_json({"Value": 0})

        def do_PUT(self):
            length = int(self.headers.get("Content-Length", 0))
            if length:
                self.rfile.read(length)
            self._send_json({"Value": EVENT_STATE})

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class _FakeMqtt:
    """Captures every publish as (topic, payload, retain)."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False, qos=0):
        self.published.append((topic, payload, retain))


def _settings(alpaca_base):
    return Settings(
        alpaca_base=alpaca_base,
        webui_base=None,  # no preview discovery in this smoke; tests the telemetry path
        config_toml_path=None,
        discovery_prefix="homeassistant",
        event_poll_sec=10,
        state_poll_sec=30,
        preview_max_px=1280,
        mqtt=MqttSettings(host="broker", port=1883, username="", password="", ssl=False),
    )


def test_assign_device_ids_slugs_and_disambiguates_collisions():
    same_name = [
        {"DeviceName": "Seestar S30 Pro", "DeviceNumber": 1},
        {"DeviceName": "Seestar S30 Pro", "DeviceNumber": 2},
        {"DeviceName": "Backyard Scope", "DeviceNumber": 3},
    ]
    ids = assign_device_ids(same_name)
    # Colliding slugs are pinned to the device number; the unique one stays clean.
    assert ids == {
        1: "seestar_s30_pro_1",
        2: "seestar_s30_pro_2",
        3: "backyard_scope",
    }


def test_build_workers_enumerates_and_publishes_discovery_per_device():
    srv, base = _serve()
    fake_mqtt = _FakeMqtt()
    try:
        workers = build_workers(alpaca=_alpaca(base), settings=_settings(base), mqtt_client=fake_mqtt)
    finally:
        srv.shutdown()

    assert [w.device_id for w in workers] == ["seestar_alpha", "seestar_beta"]

    topics = [topic for topic, _payload, _retain in fake_mqtt.published]
    # Every entity + the camera, namespaced under each device id.
    for device_id in ("seestar_alpha", "seestar_beta"):
        for entity in ENTITIES:
            assert f"homeassistant/{entity.component}/{device_id}/{entity.key}/config" in topics
        assert f"homeassistant/camera/{device_id}/preview/config" in topics
    # Discovery is retained.
    assert all(retain for _topic, _payload, retain in fake_mqtt.published)
    # Each discovery payload carries the per-scope device block.
    alpha_cfg = _find_payload(fake_mqtt, "homeassistant/sensor/seestar_alpha/telephoto_target/config")
    assert alpha_cfg["device"]["identifiers"] == ["seestar_alpha"]


def test_one_state_publish_per_device_from_canned_event_state():
    srv, base = _serve()
    fake_mqtt = _FakeMqtt()
    try:
        workers = build_workers(alpaca=_alpaca(base), settings=_settings(base), mqtt_client=fake_mqtt)
        fake_mqtt.published.clear()  # drop discovery; assert only the state cycle
        for worker in workers:
            state, _saved = worker._poll_once(now=1782799000.0)
            worker._mqtt.publish(worker.availability_topic, "online", retain=True)
            worker._mqtt.publish(worker.state_topic, json.dumps(state), retain=True)
    finally:
        srv.shutdown()

    for device_id in ("seestar_alpha", "seestar_beta"):
        state_topic = f"seestar/{device_id}/state"
        state_publishes = [p for p in fake_mqtt.published if p[0] == state_topic]
        assert len(state_publishes) == 1
        payload = json.loads(state_publishes[0][1])
        assert payload["telephoto_target"] == "NGC 7000"
        assert payload["telephoto_state"] == "working"
        assert payload["stacked_frames"] == 5


def _alpaca(base):
    # build_workers only uses configured_devices() off the passed-in client; the
    # real Alpaca speaks to the stub server for enumeration.
    from seestar_bridge.alpaca import Alpaca

    return Alpaca(base, 0)


def _find_payload(fake_mqtt, topic):
    for published_topic, payload, _retain in fake_mqtt.published:
        if published_topic == topic:
            return json.loads(payload)
    raise AssertionError(f"no publish for {topic}")
