"""Tests for the Phase-2 live ``/vid`` camera, gated on session ownership.

The live camera has ONE load-bearing invariant (the firmware boundary from the
Phase-2 spec): seestar_alp's ``:7556/vid`` MJPEG stream only serves real frames
to the session's OWNING client — a passive observer gets an Idle placeholder.
So the bridge must (a) only poll the imaging port while it owns the session
(a session-starting command dispatched OK through THIS bridge), (b) surface
not-owned as the camera's own availability topic reading ``offline``, and
(c) flip ownership off again on stop/park/shutdown or when the event stream
reports the View ended.

These tests drive the REAL worker paths — ``handle_command`` (paho's command
thread) for the ownership writes and one ``run()`` cycle (the poll thread) for
the reads — against a stub imaging HTTP server that serves a scripted multipart
MJPEG stream and counts every request, so "never polled the imaging port" is a
provable assertion, not an assumption.
"""
import io
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from seestar_bridge import control
from seestar_bridge.scope import (
    _SESSION_END_KEYS,
    _SESSION_START_KEYS,
    ScopeWorker,
)
from seestar_bridge.settings import (
    DEFAULT_IMAGING_PORT,
    MqttSettings,
    Settings,
    load_settings,
)

DEVICE = {"DeviceName": "Seestar Alpha", "DeviceNumber": 1}
DEVICE_ID = "seestar_alpha"

#: Event state of an actively-owned live session: the View event is 'working',
#: which must NOT clear ownership on the poll-side refresh.
EVENT_VIEW_WORKING = {"View": {"target_name": "M31", "state": "working", "mode": "star"}}

#: seestar_alp delimits its multipart MJPEG parts with this boundary
#: (device/seestar_imaging.py: ``BOUNDARY = b"\r\n--frame\r\n"``).
_BOUNDARY = b"\r\n--frame\r\n"


# --- stream builders --------------------------------------------------------------

def _part(content_type: str, body: bytes) -> bytes:
    """One multipart part exactly as seestar_alp's imaging server emits it."""
    return f"Content-Type: {content_type}\r\n\r\n".encode() + body + _BOUNDARY


def _stream(*parts: bytes) -> bytes:
    """A scripted MJPEG stream: lead with a boundary so every part is complete."""
    return _BOUNDARY + b"".join(parts)


def _tiny_jpeg() -> bytes:
    """A real (tiny) JPEG so the SOI/EOI + Pillow decode paths are exercised."""
    pil = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    pil.new("RGB", (8, 8), "black").save(buf, format="JPEG")
    return buf.getvalue()


#: Stand-in for the imaging server's large loading GIF (served when idle).
_LOADING_GIF = b"GIF89a" + b"\x00" * 64


# --- stubs ------------------------------------------------------------------------

class _ImagingStub:
    """Stub of seestar_alp's :7556 imaging server serving one scripted stream.

    Records every request path, so a test can prove the not-owned path NEVER
    contacted the imaging port (request_count == 0), not merely that no frame
    was published.
    """

    def __init__(self, stream: bytes):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with stub._lock:
                    stub.requests.append(self.path)
                self.send_response(200)
                self.send_header(
                    "Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                self.wfile.write(stub._stream)

            def log_message(self, *args):
                pass

        self.requests: list[str] = []
        self._lock = threading.Lock()
        self._stream = stream
        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def request_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def shutdown(self):
        self._server.shutdown()


class _FakeMqtt:
    """Captures every publish as (topic, payload, retain)."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False, qos=0):
        self.published.append((topic, payload, retain))

    def payloads(self, topic):
        return [p for t, p, _ in self.published if t == topic]


class _LiveAlpaca:
    """Alpaca stand-in: scripted event poll + every /action dispatch succeeds.

    ``get_event_state`` returns the scripted event dict; any other action (the
    command dispatches; the inert slow-cadence method_sync) returns an empty
    success so ``control.dispatch`` reports OK.
    """

    def __init__(self, event_state):
        self.event_state = event_state

    def action(self, name, params=None):
        if name == "get_event_state":
            return self.event_state
        return {}

    def is_connected(self, timeout=None):
        return True

    def get(self, prop, timeout=None):
        return 0  # site lat/lon unset, park/home falsy


# --- worker / cycle plumbing -------------------------------------------------------

def _settings(imaging_port: int) -> Settings:
    # alpaca_base's HOST is what the imaging URL derives from; its port is not
    # contacted by these tests (the event poll goes through the alpaca stub).
    return Settings(
        alpaca_base="http://127.0.0.1:5555",
        webui_base=None,
        config_toml_path=None,
        discovery_prefix="homeassistant",
        event_poll_sec=10,
        state_poll_sec=30,
        preview_max_px=1280,
        log_level="info",
        mqtt=MqttSettings(host="broker", port=1883, username="", password="", ssl=False),
        imaging_port=imaging_port,
    )


def _worker(imaging_port: int, *, event_state=None) -> ScopeWorker:
    return ScopeWorker(
        alpaca=_LiveAlpaca(EVENT_VIEW_WORKING if event_state is None else event_state),
        device=DEVICE,
        settings=_settings(imaging_port),
        mqtt_client=_FakeMqtt(),
        scope_http_base=None,
        bridge_availability_topic="seestar/bridge/availability",
    )


class _StopAfterOneCycle(Exception):
    pass


def _run_one_cycle(worker, monkeypatch):
    """Drive ``worker.run()`` through exactly one poll cycle, then stop."""
    def _stop(_seconds):
        raise _StopAfterOneCycle

    monkeypatch.setattr("seestar_bridge.scope.time.sleep", _stop)
    with pytest.raises(_StopAfterOneCycle):
        worker.run()


def _cmd_topic(key: str) -> str:
    return f"seestar/{DEVICE_ID}/cmd/{key}"


def _arm(worker, *, allow_power=False):
    worker.handle_command(_cmd_topic("controls_enabled"), "ON")
    if allow_power:
        worker.handle_command(_cmd_topic("allow_power"), "ON")


def _start_session(worker):
    """Arm the gate and dispatch a session-starting command (OK) through it."""
    _arm(worker)
    worker.handle_command(_cmd_topic("start_stack"), "PRESS")


def _closed_port() -> int:
    """A localhost port with nothing listening (connection refused)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


# -- (e) discovery: the live camera is its own entity with its own topics ----------

def test_live_camera_discovery_published_with_own_topic():
    worker = _worker(_closed_port())
    worker.publish_discovery()
    cfg_topic = f"homeassistant/camera/{DEVICE_ID}/live_view/config"
    payloads = worker._mqtt.payloads(cfg_topic)
    assert payloads, "no discovery config for the live camera"
    cfg = json.loads(payloads[-1])
    assert cfg["name"] == "Live view"
    assert cfg["unique_id"] == f"{DEVICE_ID}_live_view"
    assert cfg["topic"] == f"seestar/{DEVICE_ID}/live"
    # Its availability ANDs bridge liveness + scope reachability + OWNERSHIP.
    assert cfg["availability_mode"] == "all"
    topics = [entry["topic"] for entry in cfg["availability"]]
    assert topics == [
        "seestar/bridge/availability",
        worker.availability_topic,
        worker.live_availability_topic,
    ]


def test_phase1_preview_camera_is_untouched_and_independent():
    # The saved-stack preview camera keeps its own topic and does NOT gain the
    # live camera's ownership availability: the two are independent by design
    # (the saved preview works even when the phone app owns the session).
    worker = _worker(_closed_port())
    worker.publish_discovery()
    cfg_topic = f"homeassistant/camera/{DEVICE_ID}/preview/config"
    cfg = json.loads(worker._mqtt.payloads(cfg_topic)[-1])
    assert cfg["topic"] == f"seestar/{DEVICE_ID}/preview"
    topics = [entry["topic"] for entry in cfg["availability"]]
    assert worker.live_availability_topic not in topics


def test_discovery_seeds_live_availability_offline_retained():
    # A cold start owns no session, so HA must render the live camera
    # unavailable immediately (retained offline), not 'unknown'.
    worker = _worker(_closed_port())
    worker.publish_discovery()
    seeded = [(p, r) for t, p, r in worker._mqtt.published
              if t == worker.live_availability_topic]
    assert seeded == [("offline", True)]


# -- (a) not owned: never polls the imaging port, live reads offline ---------------

def test_not_owned_never_contacts_imaging_port_and_reads_offline(monkeypatch):
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 0  # firmware boundary: a non-owner never polls
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"
    assert worker._mqtt.payloads(worker.live_topic) == []


def test_refused_session_start_takes_no_ownership(monkeypatch):
    # Gate closed (not armed): the start command is REFUSED, so ownership must
    # not flip and the imaging port stays untouched.
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port)
        worker.handle_command(_cmd_topic("start_stack"), "PRESS")  # refused
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 0
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


# -- (b) session start dispatched OK -> owned -> poll grabs + publishes ------------

def test_session_start_ok_flips_ownership_and_poll_publishes_frame(monkeypatch):
    # The stream leads with the idle loading GIF (as the real server does), so
    # the grab must skip it and publish the first REAL JPEG part.
    stub = _ImagingStub(_stream(
        _part("image/gif", _LOADING_GIF),
        _part("image/jpeg", _tiny_jpeg()),
    ))
    try:
        worker = _worker(stub.port)
        _start_session(worker)
        assert worker.session_owned is True
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 1  # exactly one grab per poll cycle
    frames = worker._mqtt.payloads(worker.live_topic)
    assert len(frames) == 1
    assert frames[0][:2] == b"\xff\xd8"  # a real JPEG, not the GIF
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "online"


@pytest.mark.parametrize("start_key", sorted(_SESSION_START_KEYS))
def test_every_session_start_key_flips_ownership(start_key):
    worker = _worker(_closed_port())
    _arm(worker)
    # run_plan is a text control (a plan name); buttons take PRESS.
    payload = "tonight" if start_key == "run_plan" else "PRESS"
    worker.handle_command(_cmd_topic(start_key), payload)
    assert worker.session_owned is True


# -- (c) stop/park/shutdown dispatched OK -> ownership off -> live offline ---------

def test_stop_ok_clears_ownership_and_live_goes_offline(monkeypatch):
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port)
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
        assert stub.request_count() == 1  # owned cycle grabbed once

        worker.handle_command(_cmd_topic("stop"), "PRESS")
        assert worker.session_owned is False
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 1  # the post-stop cycle never polled again
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


@pytest.mark.parametrize("end_key", sorted(_SESSION_END_KEYS))
def test_every_session_end_key_clears_ownership(end_key):
    worker = _worker(_closed_port())
    _arm(worker, allow_power=True)  # park/shutdown are power-gated
    worker.handle_command(_cmd_topic("start_stack"), "PRESS")
    assert worker.session_owned is True
    worker.handle_command(_cmd_topic(end_key), "PRESS")
    assert worker.session_owned is False


def test_view_ended_event_clears_ownership_before_any_grab(monkeypatch):
    # Ownership confirmation: the poll refreshes the flag from the non-blocking
    # get_event_state View state. An explicitly ended View (seestar_alp's own
    # terminal states) clears ownership BEFORE the grab, so the imaging port is
    # not polled for a session that no longer exists.
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port, event_state={"View": {"state": "cancel"}})
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert worker.session_owned is False
    assert stub.request_count() == 0
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


def test_absent_view_event_keeps_ownership(monkeypatch):
    # A partial event with no View block carries no information about the
    # session; it must NOT clear ownership (only an explicit terminal state or
    # a stop-class command does).
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port, event_state={"PiStatus": {"temp": 20.0}})
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert worker.session_owned is True
    assert stub.request_count() == 1


# -- (d) malformed / oversized stream parts ----------------------------------------

def test_malformed_jpeg_part_is_skipped_and_real_frame_published(monkeypatch):
    # A part CLAIMING image/jpeg but carrying garbage (no SOI/EOI) is skipped;
    # the following real JPEG part is still found and published.
    stub = _ImagingStub(_stream(
        _part("image/jpeg", b"not actually a jpeg"),
        _part("image/jpeg", _tiny_jpeg()),
    ))
    try:
        worker = _worker(stub.port)
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    frames = worker._mqtt.payloads(worker.live_topic)
    assert len(frames) == 1 and frames[0][:2] == b"\xff\xd8"
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "online"


def test_oversized_part_is_bounded_and_cycle_reads_offline(monkeypatch):
    # A single never-ending part larger than the byte cap (e.g. a bloated
    # loading GIF) must be abandoned within bounds: no frame, live offline for
    # this cycle, worker loop alive. The cap is monkeypatched down so the test
    # doesn't stream 32 MiB, but the code path is the production one.
    cap = 256 * 1024
    monkeypatch.setattr("seestar_bridge.scope._MAX_PREVIEW_BYTES", cap)
    stub = _ImagingStub(
        _BOUNDARY + b"Content-Type: image/gif\r\n\r\n" + b"\x00" * (cap + 64 * 1024))
    try:
        worker = _worker(stub.port)
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert worker._mqtt.payloads(worker.live_topic) == []
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


def test_grab_failure_marks_cycle_unavailable_but_telemetry_unaffected(monkeypatch):
    # Imaging server down (connection refused): the grab failure is logged and
    # this cycle's live availability reads offline, while the telemetry state +
    # scope availability publish exactly as always.
    worker = _worker(_closed_port())
    _start_session(worker)
    _run_one_cycle(worker, monkeypatch)
    assert worker.session_owned is True  # a grab failure is not a lost session
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"
    assert worker._mqtt.payloads(worker.availability_topic)[-1] == "online"
    state = json.loads(worker._mqtt.payloads(worker.state_topic)[-1])
    assert state["telephoto_target"] == "M31"  # telemetry untouched


def test_grab_returns_none_when_stream_serves_only_the_loading_gif(monkeypatch):
    # The idle placeholder alone never yields a frame: grab returns None and
    # the cycle reads offline (matches the Phase-1 grab_preview contract).
    stub = _ImagingStub(_stream(_part("image/gif", _LOADING_GIF)))
    try:
        worker = _worker(stub.port)
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert worker._mqtt.payloads(worker.live_topic) == []
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


# -- wiring guards ------------------------------------------------------------------

def test_session_tracking_keys_exist_in_dispatch_catalog():
    # The ownership tracking keys must be REAL dispatch-catalog keys, or a
    # renamed control would silently stop flipping ownership.
    catalog = {ctl.key for ctl in control.CONTROLS}
    assert _SESSION_START_KEYS <= catalog, _SESSION_START_KEYS - catalog
    assert _SESSION_END_KEYS <= catalog, _SESSION_END_KEYS - catalog


def test_owned_grab_requests_the_device_vid_path(monkeypatch):
    # The imaging URL is /<device_num>/vid on the imaging port, derived from
    # the Alpaca host.
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port)
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.requests == ["/1/vid"]


def test_grab_gives_up_at_the_overall_deadline(monkeypatch):
    # A stream that trickles junk forever without ever completing a frame must
    # not hold the poll loop indefinitely: the grab gives up at the overall
    # deadline (patched short here) and returns None.
    timeout_sec = 0.5

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:  # endless boundary-less junk; the client must bail
                    self.wfile.write(b"\x00" * 1024)
                    time.sleep(0.01)
            except OSError:
                pass  # client hung up at its deadline

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr("seestar_bridge.scope._PREVIEW_TIMEOUT_SEC", timeout_sec)
    try:
        worker = _worker(server.server_address[1])
        started = time.monotonic()
        assert worker.grab_live_frame() is None
        assert time.monotonic() - started < timeout_sec * 10
    finally:
        server.shutdown()


# -- settings: the imaging base derivation ------------------------------------------

def _minimal_options(**extra):
    return {"alpaca_host": "davis-bridge:5555", "mqtt_host": "broker", **extra}


def test_imaging_port_defaults_to_7556():
    settings = load_settings(_minimal_options(), {})
    assert DEFAULT_IMAGING_PORT == 7556
    assert settings.imaging_port == DEFAULT_IMAGING_PORT


def test_imaging_port_option_overrides_default():
    settings = load_settings(_minimal_options(imaging_port=8756), {})
    assert settings.imaging_port == 8756
