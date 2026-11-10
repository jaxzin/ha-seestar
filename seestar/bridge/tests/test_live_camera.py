"""Tests for the Phase-2 live camera, gated on an active session.

The bridge must (a) only contact seestar_alp's imaging server while a session
is active — one it owns (a session-starting command dispatched OK through THIS
bridge) or a stack it SEES progressing (the stack count changed between its
polls, e.g. a stack started from the Seestar app), (b) serve a stack from the
cached ``snapshot.jpg`` and never from ``/vid``, keeping ``/vid`` for owned
video-mode sessions, (c) surface "no active session" as the camera's own
availability topic reading ``offline``, and (d) flip ownership off again on
stop/park/shutdown or when the event stream reports the View ended.

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
from seestar_bridge import scope as scope_mod
from seestar_bridge.scope import (
    _MAX_POLLS_WITHOUT_VIEW,
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

#: Event state of an owned live session: the View event is 'working' (which
#: must NOT clear ownership on the poll-side refresh) in a video mode, where
#: the live camera grabs /vid. Stacks are covered by EVENT_STACKING below.
EVENT_VIEW_WORKING = {"View": {"target_name": "M31", "state": "working", "mode": "scenery"}}

#: A View that ended (seestar_alp terminal state): no session is active.
EVENT_VIEW_ENDED = {"View": {"target_name": "M31", "state": "cancel", "mode": "star"}}

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
    """Stub of seestar_alp's :7556 imaging server.

    ``/<dev>/vid`` serves one scripted MJPEG stream; every request is recorded
    in ``requests``, so a test can prove a path NEVER opened /vid
    (request_count == 0), not merely that no frame was published.
    ``/<dev>/snapshot.jpg`` answers from ``snapshot`` — ``None`` is a
    seestar_alp without the endpoint (404) — and records each request's
    If-None-Match in ``snapshot_requests``.
    """

    def __init__(self, stream: bytes, snapshot: "_Snapshot | None" = None):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path.endswith("/snapshot.jpg"):
                    self._snapshot()
                    return
                with stub._lock:
                    stub.requests.append(self.path)
                self.send_response(200)
                self.send_header(
                    "Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.end_headers()
                self.wfile.write(stub._stream)

            def _snapshot(self):
                if_none_match = self.headers.get("If-None-Match")
                with stub._lock:
                    stub.snapshot_requests.append(if_none_match)
                    snap = stub.snapshot
                if snap is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if snap.status != 200:
                    self.send_response(snap.status)
                    self.end_headers()
                    return
                if if_none_match == snap.etag:
                    self.send_response(304)
                    self.send_header("ETag", snap.etag)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("ETag", snap.etag)
                self.send_header("X-Seestar-Stack-Frame", str(snap.stack_frame))
                self.send_header("Content-Length", str(len(snap.body)))
                self.end_headers()
                self.wfile.write(snap.body)

            def log_message(self, *args):
                pass

        self.requests: list[str] = []
        self.snapshot_requests: list[str | None] = []
        self.snapshot = snapshot
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


class _Snapshot:
    """One scripted snapshot.jpg answer (status, ETag, stack-count header)."""

    def __init__(self, *, stack_frame: int, etag: str = '"7"', status: int = 200):
        self.stack_frame = stack_frame
        self.etag = etag
        self.status = status
        self.body = _tiny_jpeg()


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
    _run_cycles(worker, monkeypatch, 1)


def _run_cycles(worker, monkeypatch, count):
    """Drive ``worker.run()`` through exactly ``count`` poll cycles, then stop.

    The stop is raised from the inter-cycle ``time.sleep`` — OUTSIDE the run
    loop's unkillable-cycle catch-all — so it still terminates the loop.
    """
    remaining = {"cycles": count}

    def _stop(_seconds):
        remaining["cycles"] -= 1
        if remaining["cycles"] <= 0:
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
    assert cfg["json_attributes_topic"] == worker.live_attributes_topic
    # Its availability ANDs bridge liveness + scope reachability + OWNERSHIP.
    assert cfg["availability_mode"] == "all"
    topics = [entry["topic"] for entry in cfg["availability"]]
    assert topics == [
        "seestar/bridge/availability",
        worker.availability_topic,
        worker.live_availability_topic,
    ]


def test_each_published_image_is_followed_by_a_new_version_attribute(monkeypatch):
    # HA keeps the camera's entity_picture URL fixed when only the image
    # changes; the version attribute (published AFTER the image) is what tells
    # a dashboard to refetch. An unchanged snapshot (304) publishes neither.
    stub = _ImagingStub(b"", snapshot=_Snapshot(stack_frame=150, etag='"7"'))
    clock = {"now": 1_000.0}
    monkeypatch.setattr("seestar_bridge.scope.time.time", lambda: clock["now"])
    try:
        worker = _worker(stub.port, event_state=EVENT_STACKING)
        _see_stack_progress(worker, monkeypatch)
        _run_one_cycle(worker, monkeypatch)
        stub.snapshot = _Snapshot(stack_frame=150, etag='"8"')
        clock["now"] += 60
        _run_one_cycle(worker, monkeypatch)
        _run_one_cycle(worker, monkeypatch)  # unchanged: 304
    finally:
        stub.shutdown()
    live = [t for t, _p, _r in worker._mqtt.published
            if t in (worker.live_topic, worker.live_attributes_topic)]
    assert live == [worker.live_topic, worker.live_attributes_topic] * 2
    versions = [json.loads(p)["image_updated"]
                for p in worker._mqtt.payloads(worker.live_attributes_topic)]
    assert versions == [1_000_000, 1_060_000]


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


# -- (a) no active session: never polls the imaging port, live reads offline -------

@pytest.mark.parametrize("event_state", [
    EVENT_VIEW_ENDED,
    {"View": {"state": "complete"}},
    {"PiStatus": {"temp": 20.0}},  # no View block at all (driver restarted)
])
def test_no_active_session_never_contacts_imaging_port_and_reads_offline(
        monkeypatch, event_state):
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port, event_state=event_state)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 0  # /vid would serve only a stale image + Idle
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"
    assert worker._mqtt.payloads(worker.live_topic) == []


def test_refused_session_start_takes_no_ownership(monkeypatch):
    # Gate closed (not armed): the start command is REFUSED, so ownership must
    # not flip and, with no session active, the imaging port stays untouched.
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port, event_state=EVENT_VIEW_ENDED)
        worker.handle_command(_cmd_topic("start_stack"), "PRESS")  # refused
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert worker.session_owned is False
    assert stub.request_count() == 0
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


# -- (a2) a stack in progress, whoever started it, is served from the snapshot -----

def _stack_event(stacked, *, dropped=10, view_state="working"):
    """A deep-sky stack: star View plus the Stack block's frame counts."""
    return {
        "View": {"target_name": "M31", "state": view_state, "mode": "star"},
        "Stack": {"stacked_frame": stacked, "dropped_frame": dropped},
    }


#: A deep-sky stack at 140 stacked + 10 dropped.
EVENT_STACKING = _stack_event(140)


def _see_stack_progress(worker, monkeypatch, stacked=140):
    """One baseline cycle at ``stacked - 1``, then the count moves to
    ``stacked``: the next cycle sees a stack in progress."""
    worker._alpaca.event_state = _stack_event(stacked - 1)
    _run_one_cycle(worker, monkeypatch)
    worker._alpaca.event_state = _stack_event(stacked)


def test_first_stack_count_seen_is_only_a_baseline(monkeypatch):
    # A bridge (re)start mid-stack sees one count: no progress yet, so an
    # unowned stack is not followed until the count actually moves.
    stub = _ImagingStub(b"", snapshot=_Snapshot(stack_frame=150))
    try:
        worker = _worker(stub.port, event_state=EVENT_STACKING)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.snapshot_requests == []
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


def test_unowned_stack_follows_the_snapshot_until_the_view_ends(monkeypatch):
    # A stack started from the Seestar app: the bridge dispatched nothing, but
    # it sees the count move, so the camera publishes the cached stack
    # snapshot — never opening /vid — until the View ends.
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())),
                        snapshot=_Snapshot(stack_frame=150))
    try:
        worker = _worker(stub.port, event_state=EVENT_STACKING)
        _see_stack_progress(worker, monkeypatch)
        _run_one_cycle(worker, monkeypatch)
        assert worker.session_owned is False
        assert worker._mqtt.payloads(worker.live_topic)[-1][:2] == b"\xff\xd8"
        assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "online"

        worker._alpaca.event_state = _stack_event(140, view_state="cancel")
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 0         # /vid never opened for the stack
    assert len(stub.snapshot_requests) == 1  # the ended cycle never polled
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


def test_a_stack_that_stops_progressing_goes_offline(monkeypatch):
    stub = _ImagingStub(b"", snapshot=_Snapshot(stack_frame=150))
    clock = {"now": 1000.0}
    monkeypatch.setattr("seestar_bridge.scope.time.monotonic", lambda: clock["now"])
    try:
        worker = _worker(stub.port, event_state=EVENT_STACKING)
        _see_stack_progress(worker, monkeypatch)
        _run_one_cycle(worker, monkeypatch)
        assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "online"

        clock["now"] += scope_mod._MIN_STACK_FRESH_SEC + 1  # no new sub-frame
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert len(stub.snapshot_requests) == 1
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


def test_unchanged_snapshot_costs_a_304_and_stays_online(monkeypatch):
    stub = _ImagingStub(b"", snapshot=_Snapshot(stack_frame=150, etag='"7"'))
    try:
        worker = _worker(stub.port, event_state=EVENT_STACKING)
        _see_stack_progress(worker, monkeypatch)
        _run_cycles(worker, monkeypatch, 2)
    finally:
        stub.shutdown()
    assert stub.snapshot_requests == [None, '"7"']
    assert len(worker._mqtt.payloads(worker.live_topic)) == 1  # published once
    assert worker._mqtt.payloads(worker.live_availability_topic)[-2:] == [
        "online", "online"]


def test_snapshot_from_a_previous_stack_is_rejected(monkeypatch):
    # A new stack (count 5) while seestar_alp still holds the last session's
    # image (count 287): never show the old stack as the current one.
    stub = _ImagingStub(b"", snapshot=_Snapshot(stack_frame=287))
    try:
        worker = _worker(stub.port, event_state=EVENT_STACKING)
        _see_stack_progress(worker, monkeypatch, stacked=5)
        worker._alpaca.event_state = _stack_event(5, dropped=0)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert len(stub.snapshot_requests) == 1
    assert worker._mqtt.payloads(worker.live_topic) == []
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


@pytest.mark.parametrize("snapshot", [None, _Snapshot(stack_frame=150, status=503)])
def test_unowned_stack_without_a_snapshot_reads_offline_and_never_opens_vid(
        monkeypatch, snapshot):
    # No snapshot endpoint (an older seestar_alp) or no image held yet: offline,
    # and an unowned stack never falls back to /vid.
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())), snapshot=snapshot)
    try:
        worker = _worker(stub.port, event_state=EVENT_STACKING)
        _see_stack_progress(worker, monkeypatch)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 0
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


def test_owned_stack_uses_the_snapshot_not_vid(monkeypatch):
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())),
                        snapshot=_Snapshot(stack_frame=150))
    try:
        worker = _worker(stub.port, event_state=EVENT_STACKING)
        _start_session(worker)
        _see_stack_progress(worker, monkeypatch)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 0
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "online"


def test_owned_star_session_before_stacking_never_opens_vid(monkeypatch):
    # An owned deep-sky session that is not stacking yet (slewing, plate
    # solving): /vid would run the heavy stack generator, so nothing is polled.
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())),
                        snapshot=_Snapshot(stack_frame=150))
    try:
        worker = _worker(stub.port, event_state={
            "View": {"target_name": "M31", "state": "working", "mode": "star"}})
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 0
    assert stub.snapshot_requests == []
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


_EVENT_PLANET_WORKING = {"View": {"state": "working", "mode": "planet"}}


def test_owned_video_mode_session_grabs_vid(monkeypatch):
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())),
                        snapshot=_Snapshot(stack_frame=150))
    try:
        worker = _worker(stub.port, event_state=_EVENT_PLANET_WORKING)
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 1
    assert stub.snapshot_requests == []
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "online"


def test_unowned_video_mode_session_is_not_polled(monkeypatch):
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())),
                        snapshot=_Snapshot(stack_frame=150))
    try:
        worker = _worker(stub.port, event_state=_EVENT_PLANET_WORKING)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert stub.request_count() == 0
    assert stub.snapshot_requests == []
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


def test_unreachable_scope_stops_following_an_observed_stack(monkeypatch):
    # An event-poll failure says nothing about the stack: earlier progress must
    # not keep the imaging server polled while the scope is unreachable.
    stub = _ImagingStub(b"", snapshot=_Snapshot(stack_frame=150))
    try:
        worker = _worker(stub.port, event_state=EVENT_STACKING)
        _see_stack_progress(worker, monkeypatch)
        _run_one_cycle(worker, monkeypatch)
        assert len(stub.snapshot_requests) == 1

        def _fail(name, params=None):
            raise RuntimeError("event poll failed")

        monkeypatch.setattr(worker._alpaca, "action", _fail)
        monkeypatch.setattr(worker._alpaca, "is_connected", lambda timeout=None: False)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert len(stub.snapshot_requests) == 1
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
    # goto dispatches from the stored coordinate inputs; seed them so its
    # dispatch is OK (a refused goto must not — and does not — flip ownership).
    worker.handle_command(_cmd_topic("goto_ra"), "5.591")
    worker.handle_command(_cmd_topic("goto_dec"), "-5.39")
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
        worker._alpaca.event_state = EVENT_VIEW_ENDED  # the scope stopped too
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


def test_view_ended_event_clears_confirmed_ownership_before_any_grab(monkeypatch):
    # Ownership confirmation: the poll refreshes the flag from the non-blocking
    # get_event_state View state. Once the session has been observed active
    # ('working'), an explicitly ended View (seestar_alp's own terminal states)
    # clears ownership BEFORE the grab, so the imaging port is not polled for a
    # session that no longer exists.
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port)  # EVENT_VIEW_WORKING: session active
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)  # observes 'working' -> confirmed
        assert stub.request_count() == 1

        worker._alpaca.event_state = {"View": {"state": "cancel"}}
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert worker.session_owned is False
    assert stub.request_count() == 1  # the post-cancel cycle never polled
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"


def test_stale_terminal_view_after_session_start_is_ignored(monkeypatch):
    # MAJOR: get_event_state retains the LAST View of any PREVIOUS session, so
    # right after a session-start dispatch a stale 'cancel'/'complete' must NOT
    # clear the just-granted ownership — only a session that has been observed
    # active at least once may be terminal-cleared.
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port, event_state={"View": {"state": "cancel"}})
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
        assert worker.session_owned is True  # the stale terminal was ignored
        assert stub.request_count() == 1     # and the grab proceeded

        # The new session reports active: ownership is now confirmed...
        worker._alpaca.event_state = {"View": {"state": "working"}}
        _run_one_cycle(worker, monkeypatch)
        assert worker.session_owned is True

        # ...so a LATER terminal View (scheduler not working) clears it.
        worker._alpaca.event_state = {"View": {"state": "complete"}}
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert worker.session_owned is False
    assert stub.request_count() == 2  # the cleared cycle never polled


def test_plan_view_terminal_between_targets_keeps_ownership(monkeypatch):
    # MAJOR: a scheduler-driven plan passes View through terminal states BETWEEN
    # targets. While the SAME get_event_state snapshot reports the scheduler
    # 'working', a terminal View must NOT kill the live camera mid-plan; once
    # the scheduler stops AND the View is terminal, ownership clears.
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port, event_state={
            "View": {"state": "complete"}, "scheduler": {"state": "working"}})
        _arm(worker)
        worker.handle_command(_cmd_topic("run_plan"), "tonight")
        assert worker.session_owned is True

        _run_one_cycle(worker, monkeypatch)  # between targets
        assert worker.session_owned is True
        assert stub.request_count() == 1     # live camera stayed on

        worker._alpaca.event_state = {
            "View": {"state": "complete"}, "scheduler": {"state": "stopped"}}
        _run_one_cycle(worker, monkeypatch)  # plan over
    finally:
        stub.shutdown()
    assert worker.session_owned is False
    assert stub.request_count() == 1  # the post-plan cycle never polled


def test_absent_view_event_keeps_ownership(monkeypatch):
    # A partial event with no View block carries no information about the
    # session; it must NOT clear ownership (only an explicit terminal state, a
    # stop-class command, or the sustained-absence staleness bound does).
    stub = _ImagingStub(_stream(_part("image/jpeg", _tiny_jpeg())))
    try:
        worker = _worker(stub.port, event_state={"PiStatus": {"temp": 20.0}})
        _start_session(worker)
        _run_one_cycle(worker, monkeypatch)
    finally:
        stub.shutdown()
    assert worker.session_owned is True
    assert stub.request_count() == 1


# -- stuck-ownership staleness bound -------------------------------------------

#: A successful event poll with NO View block at all (driver restart wipes
#: event_state; an owned session always has a View).
_NO_VIEW_SNAPSHOT = {"PiStatus": {"temp": 20.0}}


def test_sustained_absence_of_view_clears_stuck_ownership():
    # MINOR: after _MAX_POLLS_WITHOUT_VIEW consecutive successful polls with no
    # View block at all, ownership is stale (the driver restarted out from
    # under us) and must clear — otherwise the imaging port is polled forever.
    worker = _worker(_closed_port())
    _start_session(worker)
    for _ in range(_MAX_POLLS_WITHOUT_VIEW - 1):
        worker._refresh_session_ownership(_NO_VIEW_SNAPSHOT)
    assert worker.session_owned is True  # one short of the bound: still owned
    worker._refresh_session_ownership(_NO_VIEW_SNAPSHOT)
    assert worker.session_owned is False


def test_view_presence_resets_the_no_view_staleness_counter():
    # The bound is CONSECUTIVE absences: any View block in between restarts it.
    worker = _worker(_closed_port())
    _start_session(worker)
    for _ in range(_MAX_POLLS_WITHOUT_VIEW - 1):
        worker._refresh_session_ownership(_NO_VIEW_SNAPSHOT)
    worker._refresh_session_ownership(EVENT_VIEW_WORKING)  # View seen: reset
    for _ in range(_MAX_POLLS_WITHOUT_VIEW - 1):
        worker._refresh_session_ownership(_NO_VIEW_SNAPSHOT)
    assert worker.session_owned is True


def test_no_view_polls_with_working_scheduler_do_not_count_as_stale():
    # A working scheduler is itself evidence the session lives (e.g. a plan
    # item that has not opened a View yet); those polls never count as stale.
    worker = _worker(_closed_port())
    _start_session(worker)
    for _ in range(_MAX_POLLS_WITHOUT_VIEW + 1):
        worker._refresh_session_ownership({"scheduler": {"state": "working"}})
    assert worker.session_owned is True


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


class _TruncatingImagingStub:
    """Imaging stub whose chunked reply is truncated mid-part.

    It promises another chunk after the first, then hangs up: on the client
    side ``resp.read()`` raises ``http.client.IncompleteRead`` — an
    ``HTTPException``, NOT an ``OSError`` — which is exactly the exception
    class that used to escape ``_PROBE_ERRORS`` and kill the worker thread.
    """

    def __init__(self):
        stub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # required for chunked encoding

            def do_GET(self):
                with stub._lock:
                    stub.requests.append(self.path)
                self.send_response(200)
                self.send_header(
                    "Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                part = _BOUNDARY + b"Content-Type: image/jpeg\r\n\r\n\xff\xd8"
                self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                # Promise a 0x400-byte chunk, then close without sending it.
                self.wfile.write(b"400\r\n")
                self.close_connection = True

            def log_message(self, *args):
                pass

        self.requests: list[str] = []
        self._lock = threading.Lock()
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


def test_truncated_chunked_stream_does_not_kill_the_worker_loop(monkeypatch):
    # MAJOR: IncompleteRead (http.client.HTTPException, not an OSError) from a
    # truncated reply must degrade to live-offline-this-cycle — the NEXT cycle
    # still runs and polls again, proving the worker thread survived.
    stub = _TruncatingImagingStub()
    try:
        worker = _worker(stub.port)
        _start_session(worker)
        _run_cycles(worker, monkeypatch, 2)
    finally:
        stub.shutdown()
    assert stub.request_count() == 2  # cycle 2 ran: the loop survived cycle 1
    assert worker.session_owned is True  # a transport error is not a lost session
    assert worker._mqtt.payloads(worker.live_availability_topic)[-1] == "offline"
    # Telemetry kept publishing on both cycles.
    assert len(worker._mqtt.payloads(worker.state_topic)) == 2


def test_incomplete_read_is_a_probe_error():
    # The wiring guard for the fix: IncompleteRead is an HTTPException and NOT
    # an OSError, and _PROBE_ERRORS must cover it.
    import http.client

    assert not issubclass(http.client.IncompleteRead, OSError)
    assert issubclass(http.client.HTTPException, scope_mod._PROBE_ERRORS)


class _ExplodingAlpaca(_LiveAlpaca):
    """Alpaca stand-in whose ``is_connected`` raises an arbitrary non-probe
    exception on the FIRST call only — an exception class no targeted handler
    in the cycle expects, so only the run loop's catch-all can absorb it."""

    def __init__(self, event_state):
        super().__init__(event_state)
        self.connected_calls = 0

    def is_connected(self, timeout=None):
        self.connected_calls += 1
        if self.connected_calls == 1:
            raise ZeroDivisionError("unexpected cycle bug")
        return True


def test_unexpected_cycle_exception_publishes_offline_and_loop_continues(monkeypatch):
    # MAJOR: the run loop is UNKILLABLE by any single cycle's exception: the
    # failure is logged, the scope + live camera read offline for that cycle,
    # and the next cycle runs normally.
    alpaca = _ExplodingAlpaca(EVENT_VIEW_WORKING)
    worker = ScopeWorker(
        alpaca=alpaca,
        device=DEVICE,
        settings=_settings(_closed_port()),
        mqtt_client=_FakeMqtt(),
        scope_http_base=None,
        bridge_availability_topic="seestar/bridge/availability",
    )
    _run_cycles(worker, monkeypatch, 2)
    assert alpaca.connected_calls == 2  # cycle 2 ran: the loop survived cycle 1
    # Cycle 1 published offline (the catch-all), cycle 2 recovered to online.
    assert worker._mqtt.payloads(worker.availability_topic) == ["offline", "online"]
    assert worker._mqtt.payloads(worker.live_availability_topic)[0] == "offline"
    # Cycle 2's telemetry went out as always.
    state = json.loads(worker._mqtt.payloads(worker.state_topic)[-1])
    assert state["telephoto_target"] == "M31"


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
    monkeypatch.setattr("seestar_bridge.scope._LIVE_GRAB_TIMEOUT_SEC", timeout_sec)
    try:
        worker = _worker(server.server_address[1])
        started = time.monotonic()
        assert worker.grab_live_frame() is None
        assert time.monotonic() - started < timeout_sec * 10
    finally:
        server.shutdown()


def test_live_grab_deadline_is_short_and_named():
    # MINOR: the live grab runs INLINE in the poll loop, so its deadline is a
    # short named constant (~5 s) — a frame arrives immediately when frames
    # exist — while the saved-preview download keeps its longer budget.
    assert scope_mod._LIVE_GRAB_TIMEOUT_SEC == 5
    assert scope_mod._LIVE_GRAB_TIMEOUT_SEC < scope_mod._PREVIEW_TIMEOUT_SEC


# -- settings: the imaging base derivation ------------------------------------------

def _minimal_options(**extra):
    return {"alpaca_host": "seestar-alp:5555", "mqtt_host": "broker", **extra}


def test_imaging_port_defaults_to_7556():
    settings = load_settings(_minimal_options(), {})
    assert DEFAULT_IMAGING_PORT == 7556
    assert settings.imaging_port == DEFAULT_IMAGING_PORT


def test_imaging_port_option_overrides_default():
    settings = load_settings(_minimal_options(imaging_port=8756), {})
    assert settings.imaging_port == 8756
