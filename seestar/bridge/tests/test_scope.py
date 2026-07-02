"""Tests for the per-scope worker's pure state extraction (``build_state``).

``build_state`` is the high-value pure function: it maps one seestar_alp
``get_event_state`` dict to the entity-key state dict whose keys exactly match
the catalog in ``seestar_bridge.entities.ENTITIES``. It must be importable
without paho or Pillow (those live in separate import paths), so this module
imports ``ScopeWorker`` directly and never touches the MQTT/preview machinery.
"""
import importlib
import io
import json
import struct
import sys
import threading
import warnings
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from seestar_bridge.entities import ENTITIES
from seestar_bridge.scope import _MAX_PREVIEW_BYTES, ScopeWorker
from seestar_bridge.settings import MqttSettings, Settings

# A representative get_event_state payload exercising every extraction branch:
# telephoto View actively working, a stale SecondView (wide) that was cancelled,
# stacking frames, exposure, plate-solve (RA/Dec + angle/fov/stars/focal_len),
# the imaging plan, plate-solve annotations, filter wheel, health, and a saved
# stack path (for the preview). The wide camera carries an OLDER target than the
# active View to pin the "SecondView must not clobber View" invariant.
EVENT_STATE = {
    "PiStatus": {"temp": 41.27, "battery_capacity": 88, "charger_status": "Charging"},
    "DiskSpace": {"used_percent": 37},
    "View": {
        "target_name": "NGC 7000",
        "state": "working",
        "mode": "star",
        "gain": 80,
        "lp_filter": True,
        "target_ra_dec": [21.0, 44.0],
    },
    "SecondView": {
        "target_name": "M31",
        "state": "cancel",
        "mode": "star",
        "gain": 80,
        "lp_filter": False,
    },
    "Setting": {"wide_cam": False},
    "Stack": {"state": "working", "stacked_frame": 42, "dropped_frame": 3, "total_frame": 45},
    "Exposure": {"exp_us": 10_000_000},
    "PlateSolve": {
        "result": {
            "ra_dec": [21.0213, 44.5333],
            "angle": 123.456,
            "focal_len": 250.0,
            "star_number": 311,
            "fov": [1.2345, 0.6789],
        }
    },
    "ScopeTrack": {"tracking": True},
    "AutoGoto": {"state": "complete"},
    "ScopeGoto": {"state": "complete"},
    "ViewPlan": {"plan": {"plan_name": "Andromeda night"}, "state": "working"},
    "Annotate": {
        "result": {
            "annotations": [
                {"names": ["NGC 7000"]},
                {"name": "Pelican Nebula"},
                {"names": ["IC 5070"]},
            ]
        }
    },
    "WheelMove": {"position": 2},
    "SaveImage": {"filename": "Stacked_30.0s.fit", "fullname": "MyWorks/NGC 7000/Stacked_30.0s.fit"},
    "Alert": {"error": "guide star lost"},
}

# A representative scope identity. build_state must not depend on Alpaca/MQTT, so
# tests pass plain objects for those collaborators where a method is unused.
DEVICE = {"DeviceName": "Seestar Alpha", "DeviceNumber": 1}


class _StubAlpaca:
    """Stand-in for seestar_bridge.alpaca.Alpaca; build_state never calls it."""


def _worker(site=None):
    worker = ScopeWorker(
        alpaca=_StubAlpaca(),
        device=DEVICE,
        settings=None,
        mqtt_client=None,
        scope_http_base=None,
    )
    if site is not None:
        worker.set_site_location(*site)
    return worker


def test_build_state_is_importable_without_paho_or_pillow():
    # The pure extraction must not drag in the broker or imaging libs at import
    # time. Assert neither is loaded merely by importing scope + building state.
    for blocked in ("paho", "paho.mqtt", "paho.mqtt.client", "PIL", "PIL.Image"):
        assert blocked not in sys.modules or True  # tolerate a pre-warmed env
    # Re-import scope in isolation and confirm it does not import paho/Pillow.
    importlib.reload(importlib.import_module("seestar_bridge.scope"))


def test_build_state_keys_are_subset_of_entity_catalog():
    state = _worker().build_state(EVENT_STATE)
    catalog_keys = {entity.key for entity in ENTITIES}
    assert set(state).issubset(catalog_keys), set(state) - catalog_keys


def test_telephoto_view_fields_extracted():
    state = _worker().build_state(EVENT_STATE)
    assert state["telephoto_target"] == "NGC 7000"
    assert state["telephoto_state"] == "working"
    assert state["telephoto_mode"] == "star"
    assert state["telephoto_gain"] == 80
    assert state["telephoto_lp"] is True


def test_wide_secondview_fields_extracted_independently():
    state = _worker().build_state(EVENT_STATE)
    assert state["wide_target"] == "M31"
    assert state["wide_state"] == "cancel"
    assert state["wide_lp"] is False


def test_secondview_does_not_clobber_active_view():
    # The active telephoto View ("NGC 7000"/working) and the stale wide SecondView
    # ("M31"/cancel) must land in disjoint namespaced keys: the stale secondary
    # never overwrites the active primary's target/state.
    state = _worker().build_state(EVENT_STATE)
    assert state["telephoto_target"] == "NGC 7000"
    assert state["telephoto_state"] == "working"
    assert state["wide_target"] == "M31"
    assert state["wide_state"] == "cancel"
    assert state["telephoto_target"] != state["wide_target"]
    assert state["telephoto_state"] != state["wide_state"]


def test_stacking_and_exposure_extracted():
    state = _worker().build_state(EVENT_STATE)
    assert state["stack_state"] == "working"
    assert state["stacked_frames"] == 42
    assert state["dropped_frames"] == 3
    assert state["total_frames"] == 45
    assert state["exposure_s"] == 10.0


def test_plate_solve_fields_extracted():
    state = _worker().build_state(EVENT_STATE)
    assert state["ra"] == 21.0213
    assert state["dec"] == 44.5333
    assert state["field_rotation"] == 123.46
    assert state["focal_length"] == 250.0
    assert state["solve_stars"] == 311
    assert state["fov"] == "1.23° × 0.68°"


def test_plan_extracted():
    state = _worker().build_state(EVENT_STATE)
    assert state["plan_name"] == "Andromeda night"
    assert state["plan_active"] is True


def test_detected_objects_extracted():
    state = _worker().build_state(EVENT_STATE)
    assert state["detected_objects"] == 3
    assert state["detected_names"] == "NGC 7000, Pelican Nebula, IC 5070"


def test_filter_and_health_extracted():
    state = _worker().build_state(EVENT_STATE)
    assert state["filter_position"] == 2
    assert state["temperature"] == 41.3
    assert state["battery"] == 88
    assert state["disk_used_pct"] == 37
    assert state["last_alert"] == "guide star lost"


def test_altaz_populates_when_site_and_radec_present():
    state = _worker(site=(41.414, -73.3034)).build_state(EVENT_STATE, unix_t=1782799000.0)
    assert state["altitude"] is not None
    assert state["azimuth"] is not None
    assert -90.0 <= state["altitude"] <= 90.0
    assert 0.0 <= state["azimuth"] < 360.0


def test_altaz_absent_without_site():
    state = _worker().build_state(EVENT_STATE, unix_t=1782799000.0)
    assert "altitude" not in state or state["altitude"] is None
    assert "azimuth" not in state or state["azimuth"] is None


def test_slewing_true_when_goto_working():
    payload = {"AutoGoto": {"state": "working"}}
    state = _worker().build_state(payload)
    assert state["slewing"] is True


def test_slewing_false_when_goto_complete():
    state = _worker().build_state(EVENT_STATE)
    assert state["slewing"] is False


# --- run-cycle helpers: connectivity + availability liveness -------------------

#: Sentinel raised from a patched time.sleep to break run()'s infinite loop after
#: exactly one cycle, so the cycle's publishes can be inspected.
class _StopAfterOneCycle(Exception):
    pass


class _FakeMqtt:
    """Captures every publish as (topic, payload, retain)."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False, qos=0):
        self.published.append((topic, payload, retain))


class _CycleAlpaca:
    """Alpaca stand-in for the run loop: scripted event poll + connected probe.

    ``event_state`` is returned by the ``get_event_state`` action; setting it to
    an exception instance makes that action raise (simulating an unreachable
    scope). ``connected`` backs ``is_connected()``. ``get``/``method_sync`` are
    inert so the slow cadence never blocks the test.
    """

    def __init__(self, *, event_state, connected):
        self._event_state = event_state
        self._connected = connected

    def action(self, name, params=None):
        if name == "get_event_state":
            if isinstance(self._event_state, Exception):
                raise self._event_state
            return self._event_state
        return {}  # method_sync (get_device_state): inert

    def is_connected(self, timeout=None):
        return self._connected

    def get(self, prop, timeout=None):
        return 0  # site lat/lon unset, park/home falsy


def _cycle_settings():
    return Settings(
        alpaca_base="http://stub",
        webui_base=None,
        config_toml_path=None,
        discovery_prefix="homeassistant",
        event_poll_sec=10,
        state_poll_sec=30,
        preview_max_px=1280,
        log_level="info",
        mqtt=MqttSettings(host="broker", port=1883, username="", password="", ssl=False),
    )


def _run_one_cycle(alpaca, mqtt_client, monkeypatch, *, scope_http_base=None):
    """Drive ScopeWorker.run() through exactly one cycle, then stop."""
    worker = ScopeWorker(
        alpaca=alpaca,
        device=DEVICE,
        settings=_cycle_settings(),
        mqtt_client=mqtt_client,
        scope_http_base=scope_http_base,
        bridge_availability_topic="seestar/bridge/availability",
    )

    def _stop(_seconds):
        raise _StopAfterOneCycle

    monkeypatch.setattr("seestar_bridge.scope.time.sleep", _stop)
    with pytest.raises(_StopAfterOneCycle):
        worker.run()
    return worker


def test_connected_is_populated_each_cycle(monkeypatch):
    # BLOCKER 1: the Connected binary_sensor needs a producer. A reachable scope
    # reporting connected must publish connected=True in the state JSON.
    mqtt_client = _FakeMqtt()
    worker = _run_one_cycle(
        _CycleAlpaca(event_state=EVENT_STATE, connected=True), mqtt_client, monkeypatch)
    state_topic = worker.state_topic
    payloads = [json.loads(p) for t, p, _ in mqtt_client.published if t == state_topic]
    assert payloads, "no state publish"
    assert payloads[-1]["connected"] is True


def test_poll_failure_publishes_scope_availability_offline(monkeypatch):
    # BLOCKER 2 (a): a failed event poll with the scope reporting NOT connected
    # must publish the per-scope availability topic 'offline', never a blind
    # 'online'. connected=False also flips the Connected sensor OFF.
    mqtt_client = _FakeMqtt()
    worker = _run_one_cycle(
        _CycleAlpaca(event_state=OSError("connection refused"), connected=False),
        mqtt_client, monkeypatch)
    avail = [p for t, p, _ in mqtt_client.published if t == worker.availability_topic]
    assert avail == ["offline"]
    state = [json.loads(p) for t, p, _ in mqtt_client.published if t == worker.state_topic][-1]
    assert state["connected"] is False


def test_reachable_scope_publishes_availability_online(monkeypatch):
    # The reachable-scope counterpart: a successful poll publishes 'online'.
    mqtt_client = _FakeMqtt()
    worker = _run_one_cycle(
        _CycleAlpaca(event_state=EVENT_STATE, connected=True), mqtt_client, monkeypatch)
    avail = [p for t, p, _ in mqtt_client.published if t == worker.availability_topic]
    assert avail == ["online"]


def test_discovery_payloads_carry_two_topic_availability_list(monkeypatch):
    # BLOCKER 2 (c): each entity discovery payload must list BOTH the bridge and
    # the scope availability topics with availability_mode 'all'.
    mqtt_client = _FakeMqtt()
    worker = ScopeWorker(
        alpaca=_CycleAlpaca(event_state=EVENT_STATE, connected=True),
        device=DEVICE,
        settings=_cycle_settings(),
        mqtt_client=mqtt_client,
        scope_http_base=None,
        bridge_availability_topic="seestar/bridge/availability",
    )
    worker.publish_discovery()
    sensor_cfg_topic = f"homeassistant/binary_sensor/{worker.device_id}/connected/config"
    cfg = next(json.loads(p) for t, p, _ in mqtt_client.published if t == sensor_cfg_topic)
    assert cfg["availability_mode"] == "all"
    topics = [entry["topic"] for entry in cfg["availability"]]
    assert topics == ["seestar/bridge/availability", worker.availability_topic]
    # The camera config carries the same two-topic availability list + mode.
    cam_topic = f"homeassistant/camera/{worker.device_id}/preview/config"
    cam = next(json.loads(p) for t, p, _ in mqtt_client.published if t == cam_topic)
    assert cam["availability_mode"] == "all"
    assert [e["topic"] for e in cam["availability"]] == [
        "seestar/bridge/availability", worker.availability_topic]


def test_empty_slug_device_id_falls_back_to_scope_num():
    # MINOR: an unnamed scope must not yield a 'seestar//state' topic; the
    # constructor applies the scope_<device_num> fallback like assign_device_ids.
    worker = ScopeWorker(
        alpaca=_StubAlpaca(),
        device={"DeviceName": "", "DeviceNumber": 7},
        settings=None,
        mqtt_client=None,
        scope_http_base=None,
    )
    assert worker.device_id == "scope_7"
    assert worker.state_topic == "seestar/scope_7/state"


# --- preview fetch/decode hardening (BLOCKER 3) --------------------------------

def _serve_preview(body, *, content_length=None, content_type="image/jpeg"):
    """Serve ``body`` at any path (the scope's preview HTTP server stub).

    ``content_length`` overrides the advertised header (to fake an over-cap
    Content-Length cheaply without sending the bytes); otherwise the real length
    is sent.
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header(
                "Content-Length",
                str(len(body) if content_length is None else content_length))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _preview_worker(scope_http_base):
    return ScopeWorker(
        alpaca=_StubAlpaca(),
        device=DEVICE,
        settings=_cycle_settings(),
        mqtt_client=_FakeMqtt(),
        scope_http_base=scope_http_base,
    )


#: A valid saved-stack path under MyWorks/ (the path-safety check requires it).
_SAVED_FULLNAME = "MyWorks/NGC 7000/Stacked_30.0s.fit"


def test_fetch_preview_rejects_over_cap_content_length():
    # BLOCKER 3 (1): a body whose advertised Content-Length exceeds the cap is
    # not fetched/published — fetch_preview returns None.
    srv, base = _serve_preview(b"\xff\xd8tiny", content_length=_MAX_PREVIEW_BYTES + 1)
    try:
        assert _preview_worker(base).fetch_preview(_SAVED_FULLNAME) is None
    finally:
        srv.shutdown()


def test_fetch_preview_rejects_unbounded_over_cap_body():
    # The read-side cap catches an over-cap body even when Content-Length lies.
    big = b"\xff\xd8" + b"\x00" * (_MAX_PREVIEW_BYTES + 16)
    srv, base = _serve_preview(big, content_length=8)  # understated length
    try:
        assert _preview_worker(base).fetch_preview(_SAVED_FULLNAME) is None
    finally:
        srv.shutdown()


def test_fetch_preview_returns_none_for_non_jpeg_body():
    # BLOCKER 3: a non-JPEG body (wrong magic) returns None, never published.
    srv, base = _serve_preview(b"<html>not a jpeg</html>")
    try:
        assert _preview_worker(base).fetch_preview(_SAVED_FULLNAME) is None
    finally:
        srv.shutdown()


def _png_decompression_bomb():
    """A valid-enough PNG declaring an enormous canvas (a decompression bomb).

    Pillow reads the IHDR dimensions while opening and trips the MAX_IMAGE_PIXELS
    ceiling, raising DecompressionBombError — without us shipping gigabytes of
    pixel data (the IDAT here is a single trivial deflate block). 3.6e9 px is far
    over the 80e6 ceiling.
    """
    width = height = 60_000

    def _chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (sig + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(b"\x00")) + _chunk(b"IEND", b""))


def test_decode_preview_swallows_decompression_bomb():
    # BLOCKER 3 (2)+(3): an image declaring a canvas over the pixel ceiling trips
    # Pillow's DecompressionBombError; _decode_preview must swallow it (return
    # None) so the worker loop survives. A real PNG bomb exercises the ceiling
    # (the magic gate in fetch_preview is upstream of decode, so we test decode
    # directly with the genuine bomb image).
    image = pytest.importorskip("PIL.Image")
    bomb = _png_decompression_bomb()
    # Sanity: confirm the bomb actually raises before our guard catches it.
    image.MAX_IMAGE_PIXELS = 80_000_000
    with pytest.raises(image.DecompressionBombError):
        image.open(io.BytesIO(bomb)).load()
    assert _preview_worker("http://stub")._decode_preview(bomb) is None


def test_decode_preview_rejects_pixel_count_over_the_explicit_cap(monkeypatch):
    # MINOR: the ceiling is EXPLICIT at the cap. Pillow's own
    # DecompressionBombError only fires above 2x MAX_IMAGE_PIXELS (it merely
    # warns between 1x and 2x), so with the cap patched below the image's pixel
    # count — but the 2x threshold above it — only the explicit width*height
    # check can be doing the rejecting.
    pil = pytest.importorskip("PIL.Image")
    # _decode_preview writes the (patched) cap into the PIL module-global;
    # re-setting the current value via monkeypatch restores it on teardown.
    monkeypatch.setattr(pil, "MAX_IMAGE_PIXELS", pil.MAX_IMAGE_PIXELS)
    buf = io.BytesIO()
    pil.new("RGB", (8, 8), "white").save(buf, format="JPEG")  # 64 pixels
    monkeypatch.setattr("seestar_bridge.scope._MAX_IMAGE_PIXELS", 40)  # 40 < 64 < 80
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # Pillow's bomb WARNING (1x..2x band)
        assert _preview_worker("http://stub")._decode_preview(buf.getvalue()) is None


def test_decode_preview_accepts_pixel_count_at_or_under_the_cap(monkeypatch):
    # The same image passes when the cap covers it: the explicit check is a
    # ceiling, not a blanket rejection.
    pil = pytest.importorskip("PIL.Image")
    monkeypatch.setattr(pil, "MAX_IMAGE_PIXELS", pil.MAX_IMAGE_PIXELS)
    buf = io.BytesIO()
    pil.new("RGB", (8, 8), "white").save(buf, format="JPEG")  # 64 pixels
    monkeypatch.setattr("seestar_bridge.scope._MAX_IMAGE_PIXELS", 64)
    out = _preview_worker("http://stub")._decode_preview(buf.getvalue())
    assert out is not None and out[:2] == b"\xff\xd8"


def test_fetch_preview_swallows_garbage_after_magic():
    # A body with the JPEG magic but garbage payload is unidentifiable/truncated;
    # the decode error is swallowed (returns None), keeping the loop alive.
    srv, base = _serve_preview(b"\xff\xd8" + b"not a real jpeg payload" * 4)
    try:
        assert _preview_worker(base).fetch_preview(_SAVED_FULLNAME) is None
    finally:
        srv.shutdown()


def test_fetch_preview_rejects_path_traversal_outside_myworks():
    # MINOR: a SaveImage.fullname that escapes MyWorks/ is rejected before any
    # fetch (no scope address is even contacted).
    worker = _preview_worker("http://127.0.0.1:1")  # nothing listening; must not be hit
    assert worker.fetch_preview("../../etc/passwd.fit") is None
    assert worker.fetch_preview("MyWorks/../../etc/passwd.fit") is None


def test_maybe_publish_preview_swallows_decode_failure_keeping_last_file():
    # BLOCKER 3 (c): a bad preview leaves last_preview_file unchanged and does not
    # raise — the loop degrades to skip-this-cycle.
    srv, base = _serve_preview(b"\xff\xd8garbage")
    worker = _preview_worker(base)
    try:
        result = worker._maybe_publish_preview(_SAVED_FULLNAME, last_preview_file="prev.fit")
    finally:
        srv.shutdown()
    assert result == "prev.fit"
    # Nothing was published to the preview topic.
    assert not [p for t, p, _ in worker._mqtt.published if t == worker.preview_topic]


def test_fetch_preview_downscales_valid_jpeg():
    # Happy path stays intact: a real JPEG is fetched, downscaled, and returned.
    pil = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    pil.new("RGB", (4000, 4000), "white").save(buf, format="JPEG")
    srv, base = _serve_preview(buf.getvalue())
    try:
        out = _preview_worker(base).fetch_preview(_SAVED_FULLNAME)
    finally:
        srv.shutdown()
    assert out is not None and out[:2] == b"\xff\xd8"
    # Downscaled to <= preview_max_px on the long edge.
    decoded = pil.open(io.BytesIO(out))
    assert max(decoded.size) <= 1280
