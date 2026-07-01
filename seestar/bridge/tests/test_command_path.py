"""Integration tests for the Phase-2 MQTT command path + per-scope safety gate.

THIS CODE MOVES A PHYSICAL TELESCOPE, so these tests assert the strongest safety
property end-to-end: an inbound command only reaches the scope's ``/action``
endpoint when the per-scope gate allows it, and a command for one device never
touches another. They exercise the real wiring — ``build_workers`` (enumerate ->
publish discovery), ``subscribe_commands`` / ``make_command_router`` (the shared
client's subscribe + route), and ``ScopeWorker.handle_command`` -> ``control``
-> ``Alpaca.action`` — against:

- a stub seestar_alp ``http.server`` (like ``test_main_smoke``) that records
  every ``PUT /api/v1/telescope/{n}/action`` PER DEVICE NUMBER, so we can prove a
  command reached device A's ``/action`` and NOT device B's; and
- a fake MQTT client that captures both ``subscribe`` calls and ``publish`` calls
  and can inject an inbound message the way paho's ``on_message`` would.

The command payload never goes through a real broker: we drive the router the
bridge registered with ``set_router`` directly, which is exactly what paho's
network thread does on delivery.
"""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

from seestar_bridge.entities import control_state_topic
from seestar_bridge.main import build_workers, make_command_router, subscribe_commands
from seestar_bridge.settings import MqttSettings, Settings

DEVICES = [
    {"DeviceName": "Seestar Alpha", "DeviceNumber": 1},
    {"DeviceName": "Seestar Beta", "DeviceNumber": 2},
]

# Canned event state so the (unused-here) telemetry path stays happy if touched.
EVENT_STATE = {"View": {"target_name": "NGC 7000", "state": "working"}}

ALPHA_ID = "seestar_alpha"
BETA_ID = "seestar_beta"


class _StubAlp:
    """Stub seestar_alp recording each ``/action`` PUT keyed by device number.

    ``actions_for(device_num)`` returns the ordered ``(action_name, params)`` list
    that reached that device's ``/action`` endpoint, so a test can assert both
    that the right call landed on device A and that NOTHING landed on device B.
    """

    def __init__(self):
        self.actions: dict[int, list[tuple[str, dict]]] = {}
        self._lock = threading.Lock()
        stub = self

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
                self._send_json({"Value": 0})  # unset GPS / generic property

            def do_PUT(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b"{}"
                device_num = self._device_num(self.path)
                body = json.loads(raw.decode())
                action = body.get("Action")
                params = json.loads(body.get("Parameters", "{}"))
                with stub._lock:
                    stub.actions.setdefault(device_num, []).append((action, params))
                self._send_json({"Value": {"result": "ok"}})

            @staticmethod
            def _device_num(path):
                # /api/v1/telescope/{n}/action
                parts = urllib.parse.urlparse(path).path.split("/")
                return int(parts[parts.index("telescope") + 1])

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def actions_for(self, device_num):
        with self._lock:
            return list(self.actions.get(device_num, []))

    def shutdown(self):
        self._server.shutdown()


class _FakeMqtt:
    """Captures publishes + subscribes and can inject an inbound command message.

    ``publish`` mirrors the smoke-test fake. ``subscribe`` records the topic
    filters the bridge asked for (so we can assert the subscribe-on-connect
    contract). ``on_message`` is the hook ``set_router`` installs; ``inject``
    calls it exactly as paho's network thread would on delivery.
    """

    def __init__(self):
        self.published: list[tuple[str, object, bool]] = []
        self.subscriptions: list[str] = []
        self.on_connect = None
        self.on_message = None

    def publish(self, topic, payload, retain=False, qos=0):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)

    def inject(self, topic, payload):
        """Deliver an inbound command the way paho would (bytes payload)."""
        assert self.on_message is not None, "router not installed"
        message = _Message(topic, payload.encode("utf-8"))
        self.on_message(self, None, message)

    def retained_state(self, topic):
        """Latest retained payload published to ``topic`` (or None)."""
        matches = [p for t, p, r in self.published if t == topic and r]
        return matches[-1] if matches else None


class _Message:
    """Minimal paho MQTTMessage stand-in (topic + raw bytes payload)."""

    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


def _settings(alpaca_base):
    return Settings(
        alpaca_base=alpaca_base,
        webui_base=None,
        config_toml_path=None,
        discovery_prefix="homeassistant",
        event_poll_sec=10,
        state_poll_sec=30,
        preview_max_px=1280,
        log_level="info",
        mqtt=MqttSettings(host="broker", port=1883, username="", password="", ssl=False),
    )


def _alpaca(base):
    from seestar_bridge.alpaca import Alpaca

    return Alpaca(base, 0)


def _wire(stub, fake_mqtt):
    """Build both workers and install the command router; return the workers."""
    workers = build_workers(
        alpaca=_alpaca(stub.base), settings=_settings(stub.base), mqtt_client=fake_mqtt)
    subscribe_commands(fake_mqtt, workers)
    return workers


def _cmd_topic(device_id, key):
    return f"seestar/{device_id}/cmd/{key}"


def _arm(fake_mqtt, device_id, *, allow_power=False):
    """Arm a scope's gate by injecting the safety-switch ON command(s)."""
    fake_mqtt.inject(_cmd_topic(device_id, "controls_enabled"), "ON")
    if allow_power:
        fake_mqtt.inject(_cmd_topic(device_id, "allow_power"), "ON")


# -- (a) subscribe to the command topics on connect -------------------------------

def test_subscribes_to_every_scope_command_filter_on_connect():
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
    finally:
        stub.shutdown()
    # One cmd/# filter per scope, subscribed on the initial connect.
    assert f"seestar/{ALPHA_ID}/cmd/#" in fake_mqtt.subscriptions
    assert f"seestar/{BETA_ID}/cmd/#" in fake_mqtt.subscriptions


def test_on_connect_resubscribes_all_filters_on_reconnect():
    # The router installs on_connect so a paho reconnect re-establishes every
    # subscription; simulate a successful CONNACK and assert the filters return.
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
        fake_mqtt.subscriptions.clear()
        assert fake_mqtt.on_connect is not None
        fake_mqtt.on_connect(fake_mqtt, None, {}, 0)  # reason_code 0 == accepted
    finally:
        stub.shutdown()
    assert f"seestar/{ALPHA_ID}/cmd/#" in fake_mqtt.subscriptions
    assert f"seestar/{BETA_ID}/cmd/#" in fake_mqtt.subscriptions


# -- (b) armed command issues the right /action -----------------------------------

def test_start_live_view_armed_issues_iscope_start_view():
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
        _arm(fake_mqtt, ALPHA_ID)
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "start_live_view"), "moon")
    finally:
        stub.shutdown()

    calls = stub.actions_for(1)
    assert len(calls) == 1
    action, params = calls[0]
    assert action == "method_sync"
    assert params["method"] == "iscope_start_view"
    assert params["params"]["mode"] == "moon"


# -- (c) same command with controls disabled issues NO /action --------------------

def test_start_live_view_gated_issues_no_action():
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
        # Gate is OFF by default: do NOT arm. The command must be refused with no
        # /action reaching the scope at all.
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "start_live_view"), "moon")
    finally:
        stub.shutdown()

    assert stub.actions_for(1) == []  # CRITICAL: nothing reached the scope


# -- (d) Park needs BOTH switches -------------------------------------------------

def test_park_needs_controls_enabled_and_allow_power():
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)

        # controls_enabled only: park (power-gated) must be refused.
        _arm(fake_mqtt, ALPHA_ID, allow_power=False)
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "park"), "PRESS")
        assert stub.actions_for(1) == []  # power gate closed

        # Now also allow power: park dispatches the documented shutdown method.
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "allow_power"), "ON")
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "park"), "PRESS")
    finally:
        stub.shutdown()

    calls = stub.actions_for(1)
    assert len(calls) == 1
    action, params = calls[0]
    assert action == "method_sync"
    assert params["method"] == "pi_shutdown"


# -- (e) per-scope isolation: a command to A never touches B -----------------------

def test_command_to_device_a_does_not_call_device_b():
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        workers = _wire(stub, fake_mqtt)
        # Arm ONLY device A; issue a command to A.
        _arm(fake_mqtt, ALPHA_ID)
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "start_stack"), "PRESS")
    finally:
        stub.shutdown()

    assert [w.device_id for w in workers] == [ALPHA_ID, BETA_ID]
    assert [name for name, _ in stub.actions_for(1)] == ["start_stack"]
    assert stub.actions_for(2) == []  # device B untouched


def test_arming_device_a_does_not_arm_device_b():
    # Gate isolation: arming A leaves B closed, so the same command to B is refused.
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
        _arm(fake_mqtt, ALPHA_ID)  # arm A only
        fake_mqtt.inject(_cmd_topic(BETA_ID, "start_stack"), "PRESS")
    finally:
        stub.shutdown()
    assert stub.actions_for(2) == []  # B's gate stayed closed


# -- (f) safety switches publish retained OFF state on startup --------------------

def test_safety_switches_publish_retained_off_on_startup():
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
    finally:
        stub.shutdown()

    for device_id in (ALPHA_ID, BETA_ID):
        base = f"seestar/{device_id}"
        for key in ("controls_enabled", "allow_power"):
            topic = control_state_topic(base, key)
            assert fake_mqtt.retained_state(topic) == "OFF", topic


# -- stateful reflection + failure surfacing --------------------------------------

def test_safety_switch_on_publishes_retained_on_state():
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "controls_enabled"), "ON")
    finally:
        stub.shutdown()
    topic = control_state_topic(f"seestar/{ALPHA_ID}", "controls_enabled")
    assert fake_mqtt.retained_state(topic) == "ON"
    # A safety switch is a gate, not a scope command: nothing reached /action.
    assert stub.actions_for(1) == []


def test_stateful_control_echoes_accepted_value_to_state_topic():
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
        _arm(fake_mqtt, ALPHA_ID)
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "exposure"), "30")
    finally:
        stub.shutdown()
    # The exposure /action fired AND its accepted value was echoed for HA.
    assert [name for name, _ in stub.actions_for(1)] == ["action_set_exposure"]
    topic = control_state_topic(f"seestar/{ALPHA_ID}", "exposure")
    assert fake_mqtt.retained_state(topic) == "30"


def test_out_of_range_number_issues_no_action_and_no_echo():
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
        _arm(fake_mqtt, ALPHA_ID)
        fake_mqtt.published.clear()  # focus only on this command's effects
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "exposure"), "99999")  # above max
    finally:
        stub.shutdown()
    assert stub.actions_for(1) == []  # refused before any /action
    topic = control_state_topic(f"seestar/{ALPHA_ID}", "exposure")
    assert fake_mqtt.retained_state(topic) is None  # no echo of a refused value


def test_button_command_does_not_echo_state():
    # A momentary button dispatches but has no state_topic echo.
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
        _arm(fake_mqtt, ALPHA_ID)
        fake_mqtt.published.clear()
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "start_stack"), "PRESS")
    finally:
        stub.shutdown()
    assert [name for name, _ in stub.actions_for(1)] == ["start_stack"]
    topic = control_state_topic(f"seestar/{ALPHA_ID}", "start_stack")
    assert fake_mqtt.retained_state(topic) is None


# -- guard: a bad command never crashes the shared client -------------------------

def test_state_echo_topic_is_ignored_not_dispatched():
    # The cmd/# subscription also matches our own .../state echoes; a message on
    # one must be ignored, never re-dispatched as a command.
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
        _arm(fake_mqtt, ALPHA_ID)
        state_topic = control_state_topic(f"seestar/{ALPHA_ID}", "start_stack")
        fake_mqtt.inject(state_topic, "PRESS")
    finally:
        stub.shutdown()
    assert stub.actions_for(1) == []  # the echo topic did not trigger an action


def test_unknown_control_key_issues_no_action():
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        _wire(stub, fake_mqtt)
        _arm(fake_mqtt, ALPHA_ID)
        fake_mqtt.inject(_cmd_topic(ALPHA_ID, "not_a_real_control"), "PRESS")
    finally:
        stub.shutdown()
    assert stub.actions_for(1) == []


def test_handler_exception_does_not_propagate_into_network_loop():
    # A handler that blows up must be swallowed by set_router's guard so the shared
    # client (and thus every other scope) survives a single bad command.
    from seestar_bridge.mqtt import set_router

    fake_mqtt = _FakeMqtt()

    def boom(_topic, _payload):
        raise RuntimeError("handler blew up")

    set_router(fake_mqtt, boom, ["seestar/x/cmd/#"])
    # inject() would raise if the guard let the exception through.
    fake_mqtt.inject("seestar/x/cmd/start_stack", "PRESS")


def test_non_utf8_command_payload_is_dropped_without_raising():
    from seestar_bridge.mqtt import set_router

    fake_mqtt = _FakeMqtt()
    seen = []
    set_router(fake_mqtt, lambda t, p: seen.append((t, p)), ["seestar/x/cmd/#"])
    # Deliver raw invalid UTF-8 the way paho would; the router must drop it.
    message = _Message("seestar/x/cmd/goto", b"\xff\xfe")
    fake_mqtt.on_message(fake_mqtt, None, message)
    assert seen == []  # handler never invoked on undecodable payload


def test_router_drops_command_matching_no_scope():
    # A topic owned by no worker (a stray publish) is dropped, never mis-routed to
    # an arbitrary scope — the router asks each worker whether it owns the topic.
    stub = _StubAlp()
    fake_mqtt = _FakeMqtt()
    try:
        workers = _wire(stub, fake_mqtt)
        route = make_command_router(workers)
        route("seestar/some_other_scope/cmd/start_stack", "PRESS")
    finally:
        stub.shutdown()
    assert stub.actions_for(1) == []
    assert stub.actions_for(2) == []
