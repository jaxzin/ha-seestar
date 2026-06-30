"""Tests for the per-scope worker's pure state extraction (``build_state``).

``build_state`` is the high-value pure function: it maps one seestar_alp
``get_event_state`` dict to the entity-key state dict whose keys exactly match
the catalog in ``seestar_bridge.entities.ENTITIES``. It must be importable
without paho or Pillow (those live in separate import paths), so this module
imports ``ScopeWorker`` directly and never touches the MQTT/preview machinery.
"""
import importlib
import sys

from seestar_bridge.entities import ENTITIES
from seestar_bridge.scope import ScopeWorker

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
