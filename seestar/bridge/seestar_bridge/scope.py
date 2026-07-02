"""Per-scope worker: pure state extraction + the live poll/publish loop.

One :class:`ScopeWorker` owns one telescope. :meth:`ScopeWorker.build_state` is
PURE — it maps a seestar_alp ``get_event_state`` dict to a state dict whose keys
are a SUBSET of :data:`seestar_bridge.entities.ENTITIES` (only the fields present
in the event are written; the remaining catalog keys are filled by
:meth:`extract_device_state`, the park/home probes, and the connectivity set in
:meth:`run`) — and is deliberately importable without paho or Pillow (the MQTT
factory lives in ``mqtt.py``; Pillow is lazy-imported inside the preview fetch).
Everything that was a module-global in the validated Phase-1 bridge (device
id/name, base topic, the cached GPS site) is now per-instance, so a single
process can drive a distinct HA device per scope.

:meth:`ScopeWorker.run` is the loop: tap the non-blocking event stream every
``event_poll_sec``; probe ``get_device_state`` on a slow cadence with the
Phase-1 exponential backoff (it only answers when the scope is briefly idle);
fetch + downscale the saved stack preview whenever a new ``SaveImage`` lands;
grab one live ``/vid`` frame per cycle while THIS bridge owns the session (see
the ownership notes on :meth:`ScopeWorker._track_session_ownership`); and
publish the per-scope availability ('online' only when the scope is actually
reachable) + the state JSON each cycle.
"""
from __future__ import annotations

import io
import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import control
from .altaz import radec_to_altaz
from .control import DispatchStatus
from .entities import (
    CONTROL_ENTITIES,
    ENTITIES,
    availability_list,
    control_discovery_payload,
    control_state_topic,
    device_block,
    discovery_payload,
    slug,
)

_log = logging.getLogger(__name__)

#: I/O errors a best-effort probe may raise that should degrade gracefully (skip
#: this field this cycle) rather than abort the worker loop. A timeout from a
#: busy scope is handled separately, since it drives the backoff.
_PROBE_ERRORS = (urllib.error.URLError, OSError, ValueError)

# --- extraction constants (named, not inlined, so the contract is auditable) ---

#: Microseconds / milliseconds per second, for normalising the two exposure
#: shapes the scope reports (``exp_us`` preferred, ``exp_ms`` fallback).
_MICROSECONDS_PER_SEC = 1_000_000.0
_MILLISECONDS_PER_SEC = 1000.0
_SECONDS_PER_MINUTE = 60.0

#: Rounding precision for the various derived fields, matching Phase-1 output.
_TEMP_DECIMALS = 1
_RADEC_DECIMALS = 4
_ANGLE_DECIMALS = 2
_FOCAL_LEN_DECIMALS = 1
_FOV_DECIMALS = 2
_INTEGRATION_DECIMALS = 1

#: Cap on how many catalog-object names we publish (the field is a summary, not
#: an exhaustive list).
_MAX_DETECTED_NAMES = 12

#: The event ``state`` value that marks an in-progress goto (-> slewing).
_GOTO_WORKING = "working"
#: The ViewPlan ``state`` value that marks an actively running plan.
_PLAN_WORKING = "working"

#: A site latitude/longitude of exactly 0 means "GPS not acquired yet" for the
#: scope, so we treat it as absent rather than a real equatorial fix.
_SITE_UNSET_VALUES = (None, 0)

# --- MQTT sub-topic + camera constants -----------------------------------------

_BASE_TOPIC_PREFIX = "seestar"
_STATE_SUBTOPIC = "state"
_AVAILABILITY_SUBTOPIC = "availability"
_PREVIEW_SUBTOPIC = "preview"

_PAYLOAD_AVAILABLE = "online"
_PAYLOAD_NOT_AVAILABLE = "offline"

#: Match the entity discovery: an entity (and the camera) is available only when
#: BOTH the bridge LWT topic and the per-scope availability topic report online.
_AVAILABILITY_MODE_ALL = "all"

#: Entity key for the connectivity binary_sensor; set every cycle from the
#: scope's Alpaca ``connected`` property so the sensor reflects reality.
_CONNECTED_KEY = "connected"

_CAMERA_COMPONENT = "camera"
_CAMERA_KEY = "preview"
_CAMERA_NAME = "Live stacked preview"

#: Phase-2 live camera: fed from seestar_alp's imaging server ``/vid`` MJPEG
#: stream, published on its OWN sub-topic (``seestar/<device>/live``) with its
#: OWN availability topic underneath it, so the not-owned state can gray out
#: the live camera without touching the Phase-1 saved-stack preview.
_LIVE_CAMERA_KEY = "live_view"
_LIVE_CAMERA_NAME = "Live view"
_LIVE_SUBTOPIC = "live"

#: Discovery component for the ad-hoc last-command-result sensor (published
#: directly here, like the camera, because its state topic is not the shared
#: per-scope state JSON that entities.discovery_payload assumes).
_SENSOR_COMPONENT = "sensor"

# --- command (control) path constants ------------------------------------------

#: Sub-topic under the base topic that roots every command topic
#: (``seestar/<device>/cmd/<key>``). Kept in sync with ``entities._CMD_SUBTOPIC``;
#: used to derive the per-scope subscription wildcard and to parse a control key
#: back out of an inbound command topic.
_CMD_SUBTOPIC = "cmd"
_CMD_STATE_SUBTOPIC = "state"

#: Sub-topic (NOT under cmd/, so it can never be parsed as an inbound command)
#: where every dispatch outcome is published, and the entity that surfaces it.
#: A REFUSED/ERROR dispatch must be VISIBLE to the operator, not just a bridge
#: log line — a silently dropped command on a physical telescope is a hazard.
_CMD_RESULT_SUBTOPIC = "command_result"
_CMD_RESULT_KEY = "last_command_result"
_CMD_RESULT_NAME = "Last command result"
_CMD_RESULT_ICON = "mdi:console-line"
#: HA truncates/rejects sensor states beyond 255 chars; clip the reason to fit.
_CMD_RESULT_MAX_LEN = 255

#: The two first-class safety switches. They are STATEFUL and per-worker (default
#: OFF): they are never dispatched to Alpaca — instead they hold the gate state
#: that ``control.dispatch`` consults for every OTHER command. ``controls_enabled``
#: gates all commands; ``allow_power`` additionally gates the power actions.
_CONTROLS_ENABLED_KEY = "controls_enabled"
_ALLOW_POWER_KEY = "allow_power"
_SAFETY_SWITCH_KEYS = frozenset({_CONTROLS_ENABLED_KEY, _ALLOW_POWER_KEY})

#: HA switch command/state payloads (paho delivers the raw MQTT string).
_SWITCH_ON = "ON"
_SWITCH_OFF = "OFF"

#: Keys of the control catalog that carry persistent state HA reflects (a
#: dispatched value is echoed to their state_topic). Buttons are momentary and
#: get no echo. Derived from the dispatch catalog so it can never drift from it.
_STATEFUL_DISPATCH_COMPONENTS = frozenset({
    control.COMPONENT_SWITCH,
    control.COMPONENT_SELECT,
    control.COMPONENT_NUMBER,
    control.COMPONENT_TEXT,
})

# --- session ownership (gates the live camera) -----------------------------------
#
# seestar_alp's :7556/vid MJPEG stream only serves real frames to the client
# that OWNS the imaging session; a passive observer receives an Idle
# placeholder. That firmware boundary is surfaced, not worked around: the live
# camera only polls the stream while THIS bridge owns the session, i.e. after a
# session-starting command dispatched OK through the bridge's own command path.

#: Dispatch-catalog keys whose OK dispatch makes seestar_alp (and therefore this
#: bridge) the owning client: iscope_start_view, start_stack, and the scheduler
#: run (which itself issues iscope_start_view). Guarded against catalog drift by
#: test_session_tracking_keys_exist_in_dispatch_catalog.
_SESSION_START_KEYS = frozenset({"start_live_view", "start_stack", "run_plan"})

#: Dispatch-catalog keys whose OK dispatch ends (or stows/powers off) the
#: session, clearing ownership.
_SESSION_END_KEYS = frozenset({"stop", "park", "shutdown"})

#: View event ``state`` values that mark the session as over. Ownership is
#: confirmed on every poll from the non-blocking ``get_event_state`` tap the
#: loop already makes (no extra RPC): an explicitly-ended View clears the flag;
#: an absent View block carries no information and keeps it. The set mirrors
#: seestar_alp's OWN terminal states (device/seestar_device.py
#: ``terminal_states = {"complete", "fail", "cancel"}``).
_VIEW_ENDED_STATES = frozenset({"complete", "fail", "cancel"})

# --- HTTP + backoff constants --------------------------------------------------

#: The scope writes both a .fit and a viewable .jpg under MyWorks/; we fetch the
#: .jpg sibling of the saved .fit path.
_FIT_SUFFIX = ".fit"
_JPG_SUFFIX = ".jpg"
_JPEG_MAGIC = b"\xff\xd8"
_JPEG_END_MAGIC = b"\xff\xd9"

_PREVIEW_TIMEOUT_SEC = 30
_JPEG_QUALITY = 85

#: Live MJPEG stream constants (Phase-1 validated ``grab_preview`` parser). The
#: stream lives at ``/<device_num>/vid`` on the imaging port; parts are
#: delimited by seestar_alp's boundary (device/seestar_imaging.py:
#: ``BOUNDARY = b"\r\n--frame\r\n"``), each carrying its own Content-Type
#: header block terminated by a blank line. Only an ``image/jpeg`` part with
#: real JPEG start/end markers is a frame — the idle placeholder is a (large)
#: GIF part and must never be published.
_LIVE_STREAM_PATH = "vid"
_MJPEG_BOUNDARY = b"--frame"
_MJPEG_HEADER_END = b"\r\n\r\n"
_MJPEG_JPEG_CONTENT_TYPE = b"image/jpeg"
_MJPEG_CHUNK_BYTES = 64 * 1024

#: Hard cap on the preview body we fetch + decode. The scope's stacked .jpg is a
#: few MiB; anything over this is either not the file we expect or an attempt to
#: exhaust memory, so we reject it (mirrors discovery.py's _read_capped pattern).
_MAX_PREVIEW_BYTES = 32 * 1024 * 1024

#: Pillow pixel ceiling: an image whose pixel count exceeds this raises
#: ``Image.DecompressionBombError`` promptly during decode, rather than letting a
#: maliciously small-but-huge-canvas JPEG balloon into gigabytes of RAM. Sized
#: well above a real stacked frame (the S30 sensor is well under 50 MP).
_MAX_IMAGE_PIXELS = 80_000_000

#: The path segment every legitimate saved stack lives under on the scope; we
#: reject a fetch whose derived path escapes it (no traversal outside MyWorks/).
_PREVIEW_PATH_PREFIX = "MyWorks/"
_PARENT_DIR_SEGMENT = ".."

#: Best-effort ``get_device_state`` starves seestar_alp's web UI when it times
#: out, so we back off exponentially during capture. The Phase-1 cap keeps us
#: probing every few minutes to catch an inter-frame idle gap.
_SLOW_BACKOFF_BASE = 2
_SLOW_BACKOFF_MAX_SEC = 150

#: Alpaca property names probed on the slow cadence (block ~8s during capture,
#: which is why they are off the fast event path).
_PARK_PROPS = (("at_park", "atpark"), ("at_home", "athome"))
_SITE_LAT_PROP = "sitelatitude"
_SITE_LON_PROP = "sitelongitude"

#: The method_sync RPC that returns the best-effort health/mount block.
_DEVICE_STATE_METHOD = "get_device_state"
_EVENT_STATE_ACTION = "get_event_state"


def _pillow_decode_errors() -> tuple[type[Exception], ...]:
    """Pillow's decode-failure exception types, or empty when Pillow is absent.

    Kept lazy (Pillow is imported on demand) so ``build_state`` stays importable
    without imaging libs. ``DecompressionBombError`` subclasses ``Exception`` (a
    bomb is an error here, not a warning), and ``UnidentifiedImageError``/``OSError``
    cover a truncated or non-image body. Used to harden the preview-publish path.
    """
    try:
        from PIL import Image, UnidentifiedImageError
    except ModuleNotFoundError:
        return ()
    return (Image.DecompressionBombError, UnidentifiedImageError)


def _nav(obj: Any, *path: Any, default: Any = None) -> Any:
    """Safely navigate nested dict/list by keys/indices (Phase-1 ``_nav``)."""
    for key in path:
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list) and isinstance(key, int) and -len(obj) <= key < len(obj):
            obj = obj[key]
        else:
            return default
    return obj if obj is not None else default


class ScopeWorker:
    """Owns one telescope: pure state extraction + the poll/publish loop.

    ``device`` is one entry from ``Alpaca.configured_devices()``
    (``{DeviceName, DeviceNumber, ...}``). ``scope_http_base`` is the scope's own
    HTTP address for the preview fetch (from ``discover_addresses``); when
    ``None`` the preview degrades gracefully and is skipped.
    """

    def __init__(self, alpaca, device, settings, mqtt_client, scope_http_base,
                 *, device_id=None, bridge_availability_topic=None):
        self._alpaca = alpaca
        self._device = device
        self._settings = settings
        self._mqtt = mqtt_client
        self._scope_http_base = scope_http_base.rstrip("/") if scope_http_base else None
        device_num = device.get("DeviceNumber")
        #: Device number on the imaging server too (the /vid path segment).
        self._device_num = device_num
        # Stable HA device id = slug of the scope name (caller may override to
        # disambiguate a name collision by device_num). Apply the same empty-slug
        # fallback main.assign_device_ids uses so an unnamed scope can never
        # produce a 'seestar//state' topic from a blank id.
        self._device_id = device_id or slug(device.get("DeviceName", "")) or f"scope_{device_num}"
        self._device_name = device.get("DeviceName", self._device_id)
        # Process-level liveness topic (the broker's LWT marks it offline if the
        # bridge dies); combined with the per-scope availability topic so an
        # entity is available only when both are up. Defaults to the scope's own
        # topic when unset, degrading to per-scope-only liveness.
        self._bridge_availability_topic = bridge_availability_topic or self.availability_topic
        # Observer location from the scope's own GPS; cached once, never published.
        self._site_lat: float | None = None
        self._site_lon: float | None = None
        # Per-worker safety gate (both default OFF). These are the stateful safety
        # switches; every command is gated on them via ``control.dispatch``. They
        # are isolated per worker, so arming one scope never arms another.
        self._controls_enabled = False
        self._allow_power = False
        # Session ownership (gates the live camera; see the _SESSION_* constants).
        # Written by paho's command thread (_track_session_ownership) and by the
        # poll thread (_refresh_session_ownership), read by the poll thread every
        # cycle — so all access goes through the lock, never the bare attribute.
        self._session_owned = False
        self._session_lock = threading.Lock()
        # Value-only stored inputs (imaging mode; goto target/RA/Dec): updated by
        # their command topics WITHOUT any Alpaca call, consumed by the trigger
        # buttons' dispatch. Imaging mode starts at the documented default.
        self._stored_inputs: dict[str, str] = {
            control.IMAGING_MODE_KEY: control.DEFAULT_IMAGING_MODE,
        }

    # -- identity / topics ------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def base_topic(self) -> str:
        return f"{_BASE_TOPIC_PREFIX}/{self._device_id}"

    @property
    def state_topic(self) -> str:
        return f"{self.base_topic}/{_STATE_SUBTOPIC}"

    @property
    def availability_topic(self) -> str:
        return f"{self.base_topic}/{_AVAILABILITY_SUBTOPIC}"

    @property
    def preview_topic(self) -> str:
        return f"{self.base_topic}/{_PREVIEW_SUBTOPIC}"

    @property
    def live_topic(self) -> str:
        """Where the live ``/vid`` camera's JPEG frames are published."""
        return f"{self.base_topic}/{_LIVE_SUBTOPIC}"

    @property
    def live_availability_topic(self) -> str:
        """The live camera's OWN availability topic: ``offline`` when not owned."""
        return f"{self.live_topic}/{_AVAILABILITY_SUBTOPIC}"

    @property
    def command_result_topic(self) -> str:
        """Where every dispatch outcome (ok/refused/error + reason) is published."""
        return f"{self.base_topic}/{_CMD_RESULT_SUBTOPIC}"

    @property
    def command_topic_filter(self) -> str:
        """MQTT wildcard covering every command topic this scope owns.

        ``seestar/<device>/cmd/#`` — the orchestrator subscribes the shared client
        to one such filter per scope. The trailing ``#`` deliberately also matches
        the ``.../state`` echo topics; :meth:`handle_command` ignores those so a
        retained state message we publish never loops back as a command.
        """
        return f"{self.base_topic}/{_CMD_SUBTOPIC}/#"

    def owns_topic(self, topic: str) -> bool:
        """True iff ``topic`` is a command topic under THIS scope's base topic.

        The orchestrator routes an inbound command to the owning worker by asking
        each worker whether it owns the topic. Per-scope isolation depends on this:
        a command addressed to device A must never be handed to device B.
        """
        return topic.startswith(f"{self.base_topic}/{_CMD_SUBTOPIC}/")

    def set_site_location(self, lat: float, lon: float) -> None:
        """Cache the GPS site (deg) used to turn RA/Dec into Alt/Az."""
        self._site_lat = float(lat)
        self._site_lon = float(lon)

    # -- pure extraction --------------------------------------------------------

    def build_state(self, event_state: dict, *, unix_t: float | None = None) -> dict:
        """Map one ``get_event_state`` dict -> the entity-key state dict (PURE).

        Ports the validated Phase-1 ``update_from_events`` extraction, namespaced
        per scope. Keys present in the result exactly match
        :data:`seestar_bridge.entities.ENTITIES`. Alt/Az is computed from the
        plate-solved RA/Dec + the cached GPS site (when both are known); ``unix_t``
        defaults to now so the loop need not pass it.
        """
        state: dict[str, Any] = {}
        if not isinstance(event_state, dict):
            return state

        def g(*path: Any, default: Any = None) -> Any:
            return _nav(event_state, *path, default=default)

        self._extract_health(state, g)
        self._extract_cameras(state, g)
        self._extract_stacking(state, g)
        self._extract_pointing(state, g, unix_t)
        self._extract_plan(state, g)
        self._extract_detections(state, g)
        self._extract_misc(state, g)
        return state

    @staticmethod
    def _extract_health(state, g):
        # Battery/charger here are intermittent; the full set arrives via
        # get_device_state (merged separately in the loop).
        if g("PiStatus", "temp") is not None:
            state["temperature"] = round(g("PiStatus", "temp"), _TEMP_DECIMALS)
        if g("PiStatus", "battery_capacity") is not None:
            state["battery"] = g("PiStatus", "battery_capacity")
        if g("PiStatus", "charger_status"):
            state["charger_status"] = g("PiStatus", "charger_status")
        if g("DiskSpace", "used_percent") is not None:
            state["disk_used_pct"] = g("DiskSpace", "used_percent")

    @staticmethod
    def _extract_cameras(state, g):
        # Telephoto = primary View (cam 0); Wide = SecondView (cam 1). Each reads
        # into its own namespaced fields so a stale secondary never overwrites the
        # active camera's target/state.
        for prefix, event in (("telephoto", "View"), ("wide", "SecondView")):
            if g(event, "target_name"):
                state[f"{prefix}_target"] = g(event, "target_name")
            if g(event, "state"):
                state[f"{prefix}_state"] = g(event, "state")
            if g(event, "mode"):
                state[f"{prefix}_mode"] = g(event, "mode")
            if g(event, "gain") is not None:
                state[f"{prefix}_gain"] = g(event, "gain")
            if g(event, "lp_filter") is not None:
                state[f"{prefix}_lp"] = bool(g(event, "lp_filter"))
        wide_cam = g("Setting", "wide_cam")
        if wide_cam is not None:
            state["active_camera"] = "Wide-field" if wide_cam else "Telephoto"

    @staticmethod
    def _extract_stacking(state, g):
        if g("Stack", "state"):
            state["stack_state"] = g("Stack", "state")
        for src, dst in (("stacked_frame", "stacked_frames"),
                         ("dropped_frame", "dropped_frames"),
                         ("total_frame", "total_frames")):
            if g("Stack", src) is not None:
                state[dst] = g("Stack", src)
        if g("Exposure", "exp_us") is not None:
            state["exposure_s"] = round(g("Exposure", "exp_us") / _MICROSECONDS_PER_SEC, _TEMP_DECIMALS)
        elif g("Exposure", "exp_ms") is not None:
            state["exposure_s"] = round(g("Exposure", "exp_ms") / _MILLISECONDS_PER_SEC, _TEMP_DECIMALS)
        # Integration time is derivable once both frames + exposure are known.
        if state.get("stacked_frames") and state.get("exposure_s"):
            state["integration_min"] = round(
                state["stacked_frames"] * state["exposure_s"] / _SECONDS_PER_MINUTE,
                _INTEGRATION_DECIMALS,
            )

    def _extract_pointing(self, state, g, unix_t):
        # Prefer the plate-solved position, then the goto position, then target.
        radec = (g("PlateSolve", "result", "ra_dec")
                 or g("ScopeGoto", "cur_ra_dec")
                 or g("View", "target_ra_dec"))
        if isinstance(radec, list) and len(radec) == 2:
            state["ra"] = round(radec[0], _RADEC_DECIMALS)
            state["dec"] = round(radec[1], _RADEC_DECIMALS)
        if g("ScopeTrack", "tracking") is not None:
            state["tracking"] = bool(g("ScopeTrack", "tracking"))
        if g("AutoGoto", "state"):
            state["goto_state"] = g("AutoGoto", "state")
        # Slewing is an in-progress goto derived from events (the Alpaca `slewing`
        # property blocks ~10s during capture, so it is never polled).
        state["slewing"] = _GOTO_WORKING in (g("AutoGoto", "state"), g("ScopeGoto", "state"))

        if g("PlateSolve", "result", "angle") is not None:
            state["field_rotation"] = round(g("PlateSolve", "result", "angle"), _ANGLE_DECIMALS)
        if g("PlateSolve", "result", "focal_len") is not None:
            state["focal_length"] = round(g("PlateSolve", "result", "focal_len"), _FOCAL_LEN_DECIMALS)
        if g("PlateSolve", "result", "star_number") is not None:
            state["solve_stars"] = g("PlateSolve", "result", "star_number")
        fov = g("PlateSolve", "result", "fov")
        if isinstance(fov, list) and len(fov) == 2:
            state["fov"] = f"{round(fov[0], _FOV_DECIMALS)}° × {round(fov[1], _FOV_DECIMALS)}°"

        # Alt/Az from the plate-solved RA/Dec + cached GPS site — correct during
        # capture (unlike the Alpaca alt/az, which read 0 while busy).
        if (state.get("ra") is not None and state.get("dec") is not None
                and self._site_lat is not None and self._site_lon is not None):
            when = time.time() if unix_t is None else unix_t
            state["altitude"], state["azimuth"] = radec_to_altaz(
                state["ra"], state["dec"], self._site_lat, self._site_lon, when)

    @staticmethod
    def _extract_plan(state, g):
        if g("ViewPlan", "plan", "plan_name"):
            state["plan_name"] = g("ViewPlan", "plan", "plan_name")
        if g("ViewPlan", "state"):
            state["plan_active"] = g("ViewPlan", "state") == _PLAN_WORKING

    @staticmethod
    def _extract_detections(state, g):
        annotations = g("Annotate", "result", "annotations")
        if isinstance(annotations, list):
            state["detected_objects"] = len(annotations)
            names = []
            for annotation in annotations:
                candidate = annotation.get("names") or (
                    [annotation.get("name")] if annotation.get("name") else [])
                if candidate:
                    names.append(candidate[0])
            if names:
                state["detected_names"] = ", ".join(names[:_MAX_DETECTED_NAMES])

    @staticmethod
    def _extract_misc(state, g):
        if g("WheelMove", "position") is not None:
            state["filter_position"] = g("WheelMove", "position")
        if g("SaveImage", "filename"):
            state["last_saved"] = g("SaveImage", "filename")
        if g("Alert", "error"):
            state["last_alert"] = g("Alert", "error")

    @staticmethod
    def extract_device_state(device_state: dict) -> dict:
        """Map a ``get_device_state`` dict -> the best-effort health/mount fields.

        Only answers when the scope is briefly idle; the loop merges the result
        into the published state. Ported from Phase-1 ``update_from_device_state``.
        """
        out: dict[str, Any] = {}
        if not isinstance(device_state, dict):
            return out

        def d(*path: Any, default: Any = None) -> Any:
            return _nav(device_state, *path, default=default)

        if d("mount", "equ_mode") is not None:
            out["mount_mode"] = "Equatorial" if d("mount", "equ_mode") else "Alt-Az"
        if d("focuser", "step") is not None:
            out["focuser"] = d("focuser", "step")
        if d("setting", "heater_enable") is not None:
            out["dew_heater"] = bool(d("setting", "heater_enable"))
        if d("device", "firmware_ver_string"):
            out["firmware"] = d("device", "firmware_ver_string")
        if d("pi_status", "battery_capacity") is not None:
            out["battery"] = d("pi_status", "battery_capacity")
        if d("pi_status", "charger_status"):
            out["charger_status"] = d("pi_status", "charger_status")
        return out

    # -- discovery + preview ----------------------------------------------------

    def publish_discovery(self) -> None:
        """Publish retained MQTT discovery configs for every entity + the camera.

        Namespaced under this scope's ``device_id`` so each scope appears as its
        own HA device.
        """
        block = device_block(self._device_id, self._device_name)
        prefix = self._settings.discovery_prefix
        for entity in ENTITIES:
            topic = f"{prefix}/{entity.component}/{self._device_id}/{entity.key}/config"
            payload = discovery_payload(
                entity,
                device_block=block,
                base_topic=self.base_topic,
                bridge_availability_topic=self._bridge_availability_topic,
            )
            self._mqtt.publish(topic, json.dumps(payload), retain=True)
        # Phase-2 command entities: same per-scope device, each with a
        # command_topic the bridge subscribes to (subscription is wired by the
        # orchestrator's subscribe_commands). ``control_entity`` avoids shadowing
        # the ``control`` dispatch module imported at the top.
        for control_entity in CONTROL_ENTITIES:
            topic = f"{prefix}/{control_entity.component}/{self._device_id}/{control_entity.key}/config"
            payload = control_discovery_payload(
                control_entity,
                device_block=block,
                base_topic=self.base_topic,
                bridge_availability_topic=self._bridge_availability_topic,
            )
            self._mqtt.publish(topic, json.dumps(payload), retain=True)
        camera = {
            "name": _CAMERA_NAME,
            "unique_id": f"{self._device_id}_{_CAMERA_KEY}",
            "object_id": f"{self._device_id}_{_CAMERA_KEY}",
            "topic": self.preview_topic,
            "availability": availability_list(
                self._bridge_availability_topic, self.availability_topic),
            "availability_mode": _AVAILABILITY_MODE_ALL,
            "device": block,
        }
        camera_topic = f"{prefix}/{_CAMERA_COMPONENT}/{self._device_id}/{_CAMERA_KEY}/config"
        self._mqtt.publish(camera_topic, json.dumps(camera), retain=True)
        # Phase-2 live camera: its availability list ADDS its own ownership
        # topic to the shared bridge + scope pair, so HA grays it out whenever
        # this bridge does not own the session (the :7556/vid firmware boundary)
        # while the Phase-1 saved-stack preview above stays independent.
        live_camera = {
            "name": _LIVE_CAMERA_NAME,
            "unique_id": f"{self._device_id}_{_LIVE_CAMERA_KEY}",
            "object_id": f"{self._device_id}_{_LIVE_CAMERA_KEY}",
            "topic": self.live_topic,
            "availability": availability_list(
                self._bridge_availability_topic, self.availability_topic,
                self.live_availability_topic),
            "availability_mode": _AVAILABILITY_MODE_ALL,
            "device": block,
        }
        live_camera_topic = (
            f"{prefix}/{_CAMERA_COMPONENT}/{self._device_id}/{_LIVE_CAMERA_KEY}/config")
        self._mqtt.publish(live_camera_topic, json.dumps(live_camera), retain=True)
        # Seed the live camera unavailable (retained): a cold-started bridge
        # owns no session, and HA must render that as offline, not 'unknown'.
        self._publish_live_availability(False)
        # Operator-visible dispatch outcome: a plain sensor fed by the per-scope
        # command_result topic, so a REFUSED/ERROR command shows up in HA (with
        # its reason) instead of vanishing into the bridge log.
        result_sensor = {
            "name": _CMD_RESULT_NAME,
            "unique_id": f"{self._device_id}_{_CMD_RESULT_KEY}",
            "object_id": f"{self._device_id}_{_CMD_RESULT_KEY}",
            "state_topic": self.command_result_topic,
            "icon": _CMD_RESULT_ICON,
            "availability": availability_list(
                self._bridge_availability_topic, self.availability_topic),
            "availability_mode": _AVAILABILITY_MODE_ALL,
            "device": block,
        }
        result_topic = (
            f"{prefix}/{_SENSOR_COMPONENT}/{self._device_id}/{_CMD_RESULT_KEY}/config")
        self._mqtt.publish(result_topic, json.dumps(result_sensor), retain=True)
        # Seed the two safety switches to a known, retained OFF so HA renders them
        # correctly on a cold start (a switch with no retained state shows as
        # unknown). This also encodes the fail-safe default: the gate is CLOSED
        # until an operator explicitly arms it.
        self._publish_safety_state()
        # Seed the stored inputs the same way (imaging mode's default; empty goto
        # fields) so HA never renders them as 'unknown'.
        for stored in control.STORED_INPUTS:
            self._publish_control_state(
                stored.key, self._stored_inputs.get(stored.key, ""))

    def _publish_safety_state(self) -> None:
        """Publish both safety switches' current gate state (retained) to HA.

        Called once at discovery (seeding the retained OFF default) and again
        whenever a safety switch is toggled, so HA always reflects the live gate.
        """
        self._publish_control_state(_CONTROLS_ENABLED_KEY, self._controls_enabled)
        self._publish_control_state(_ALLOW_POWER_KEY, self._allow_power)

    def _publish_control_state(self, key: str, value: Any) -> None:
        """Publish one control's current value to its retained ``state_topic``.

        ``bool`` values are rendered as the HA switch ``ON``/``OFF`` payloads; any
        other value (a select mode, a number) is published as its string form so
        HA reflects the live setting.
        """
        if isinstance(value, bool):
            payload = _SWITCH_ON if value else _SWITCH_OFF
        else:
            payload = str(value)
        self._mqtt.publish(control_state_topic(self.base_topic, key), payload, retain=True)

    # -- command path -----------------------------------------------------------

    def handle_command(self, topic: str, payload: str) -> None:
        """Route one inbound command message for THIS scope's device.

        Parses the control key from ``topic`` and either:

        - **safety switch** (``controls_enabled`` / ``allow_power``): update the
          per-worker gate state and re-publish it (retained) to the switch's
          state_topic. These are NEVER dispatched to Alpaca — they only hold the
          gate the other commands are checked against.
        - **stored input** (imaging mode; goto target/RA/Dec): validate, store on
          this worker, and echo to its state_topic. NEVER dispatched — changing a
          value must not move the scope; only its trigger button consumes it.
        - **any other control**: hand it to :func:`control.dispatch` with the
          CURRENT gate state, so no command reaches the scope unless the gate
          allows it. A stateful control's new value is echoed to its state_topic
          on success, and EVERY outcome (ok/refused/error + reason) is published
          to :attr:`command_result_topic` so a blocked command is visible in HA,
          never silent.

        A message on a control's own ``.../state`` echo topic (which the ``#``
        subscription also matches) is ignored so our retained echoes never loop
        back as commands. This method is defensive — it is invoked from the shared
        MQTT network thread and must not raise into it — but the outer guard in
        :func:`mqtt.set_router` is the ultimate backstop.
        """
        key = self._command_key(topic)
        if key is None:
            return  # a .../state echo or a malformed topic; not a command
        if key in _SAFETY_SWITCH_KEYS:
            self._apply_safety_switch(key, payload)
            return
        if control.stored_input_for(key) is not None:
            self._apply_stored_input(key, payload)
            return
        self._dispatch_control(key, payload)

    def _command_key(self, topic: str) -> str | None:
        """Extract the control key from a command topic, or ``None`` to ignore.

        A concrete command topic is ``<base>/cmd/<key>``. The ``.../cmd/<key>/state``
        echo topic (also matched by the ``#`` subscription) has an extra segment
        and is deliberately ignored, as is any topic that isn't ours.
        """
        prefix = f"{self.base_topic}/{_CMD_SUBTOPIC}/"
        if not topic.startswith(prefix):
            return None
        remainder = topic[len(prefix):]
        if not remainder or "/" in remainder:
            # Empty (``.../cmd/``) or has a trailing segment (the ``/state`` echo).
            return None
        return remainder

    def _apply_safety_switch(self, key: str, payload: str) -> None:
        """Toggle a per-worker safety switch and re-publish its retained state.

        The gate state is authoritative in the worker; the retained state_topic is
        only HA's reflection of it. We log the transition because arming a scope is
        a safety-relevant event.
        """
        value = payload.strip().upper() == _SWITCH_ON
        if key == _CONTROLS_ENABLED_KEY:
            self._controls_enabled = value
        else:  # _ALLOW_POWER_KEY (the only other member of _SAFETY_SWITCH_KEYS)
            self._allow_power = value
        _log.info("%s: safety switch %s -> %s", self._device_id, key,
                  _SWITCH_ON if value else _SWITCH_OFF)
        self._publish_control_state(key, value)

    def _apply_stored_input(self, key: str, payload: str) -> None:
        """Store a value-only input and echo it (retained) to its state_topic.

        NO Alpaca call happens here by design: selecting an imaging mode or
        typing goto coordinates must never start a session or move the scope.
        An invalid value (a mode outside the select's options) is refused —
        logged AND surfaced on the command-result topic — and the previous
        stored value is kept.
        """
        invalid = control.validate_stored_input(key, payload)
        if invalid is not None:
            _log.warning("%s: stored input refused: %s", self._device_id, invalid)
            self._publish_command_result(DispatchStatus.REFUSED.value, invalid)
            return
        self._stored_inputs[key] = payload
        self._publish_control_state(key, payload)

    def _dispatch_control(self, key: str, payload: str) -> None:
        """Dispatch one control through the safety gate and reflect the result.

        The gate state passed here is THIS worker's, so a command for one scope can
        never be gated by (or affect) another — as is the stored-input snapshot the
        trigger buttons (goto, start live view) compose their payload from. On a
        successful dispatch of a stateful control (switch/number/text) the accepted
        value is echoed to its state_topic so HA shows the live setting; a momentary
        button is not echoed. EVERY outcome — ok, refused, or error, with its
        reason — is published to :attr:`command_result_topic` so the operator sees
        a blocked/failed command in HA; the dispatcher's log line is not the only
        trace.
        """
        result = control.dispatch(
            self._alpaca, key, payload,
            controls_enabled=self._controls_enabled,
            allow_power=self._allow_power,
            stored=self._stored_inputs,
        )
        if result.status is DispatchStatus.OK:
            self._track_session_ownership(key)
            if self._control_is_stateful(key):
                self._publish_control_state(key, payload)
        self._publish_command_result(result.status.value, result.reason or key)

    def _publish_command_result(self, status: str, detail: str) -> None:
        """Publish one dispatch outcome (retained) to the command-result topic.

        Rendered as ``<status>: <detail>`` and clipped to HA's 255-char sensor
        state limit. Retained so the last outcome survives an HA restart.
        """
        text = f"{status}: {detail}"[:_CMD_RESULT_MAX_LEN]
        self._mqtt.publish(self.command_result_topic, text, retain=True)

    @staticmethod
    def _control_is_stateful(key: str) -> bool:
        """True iff the dispatch catalog marks ``key`` as a stateful control.

        Only stateful controls (switch/select/number/text) echo their accepted
        value back to HA; a button is momentary and has no state to reflect.
        """
        ctl = control.control_for(key)
        return ctl is not None and ctl.component in _STATEFUL_DISPATCH_COMPONENTS

    # -- session ownership (gates the live camera) -------------------------------

    @property
    def session_owned(self) -> bool:
        """True while THIS bridge owns the imaging session (lock-guarded read)."""
        with self._session_lock:
            return self._session_owned

    def _set_session_owned(self, owned: bool, *, reason: str) -> None:
        """Flip the ownership flag under the lock, logging every transition.

        Ownership decides whether the imaging port is polled at all, so a
        transition is operationally significant and never silent.
        """
        with self._session_lock:
            changed = self._session_owned != owned
            self._session_owned = owned
        if changed:
            _log.info("%s: session ownership -> %s (%s)",
                      self._device_id, "owned" if owned else "not owned", reason)

    def _track_session_ownership(self, key: str) -> None:
        """Update ownership from one OK dispatch (called on paho's thread).

        A session-starting command (start_live_view / start_stack / run_plan)
        dispatched OK makes seestar_alp — and therefore this bridge — the
        owning client of the ``/vid`` stream; a stop-class command (stop /
        park / shutdown) ends that. Every other control leaves the flag alone.
        """
        if key in _SESSION_START_KEYS:
            self._set_session_owned(True, reason=f"{key} dispatched ok")
        elif key in _SESSION_END_KEYS:
            self._set_session_owned(False, reason=f"{key} dispatched ok")

    def _refresh_session_ownership(self, event_state: Any) -> None:
        """Confirm ownership against the event tap (called on the poll thread).

        Uses the ``View`` state from the non-blocking ``get_event_state`` the
        loop already polls every cycle (rather than an extra ``get_video_status``
        RPC): a View in one of seestar_alp's own terminal states means the
        session ended out from under us (phone app stop, plan completion,
        failure), so ownership is dropped — the confirmation failed. An absent
        View block carries no information and keeps the current flag.
        """
        if not self.session_owned:
            return
        view_state = _nav(event_state, "View", "state")
        if isinstance(view_state, str) and view_state.lower() in _VIEW_ENDED_STATES:
            self._set_session_owned(False, reason=f"View state {view_state!r}")

    def fetch_preview(self, fullname: str) -> bytes | None:
        """Fetch the saved stacked .jpg from the scope's HTTP server, downscaled.

        Pillow is lazy-imported here so ``build_state`` stays importable without
        it. Returns the downscaled JPEG bytes (the caller publishes them), or
        ``None`` if no scope address is known, the derived path escapes
        ``MyWorks/``, the body is over-cap or not a JPEG, or the decode fails.
        When Pillow is unavailable it RETURNS the raw (full-size) bytes for the
        caller to publish. The fetch is bounded to :data:`_MAX_PREVIEW_BYTES` and
        the decode to :data:`_MAX_IMAGE_PIXELS`, so neither an oversized body nor
        a decompression-bomb JPEG can exhaust the worker's memory.
        """
        if not self._scope_http_base:
            return None
        jpg_path = fullname.rsplit(".", 1)[0] + _JPG_SUFFIX
        if not self._preview_path_is_safe(jpg_path):
            _log.warning("%s: rejecting preview path outside %s: %r",
                         self._device_id, _PREVIEW_PATH_PREFIX, jpg_path)
            return None
        # quote with safe='' so every '/' is escaped: the scope-provided path is
        # untrusted, and a stray segment must not let the URL walk the server.
        url = f"{self._scope_http_base}/{urllib.parse.quote(jpg_path, safe='')}"
        raw = self._fetch_capped(url)
        if raw is None or raw[:len(_JPEG_MAGIC)] != _JPEG_MAGIC:
            return None
        return self._decode_preview(raw)

    @staticmethod
    def _preview_path_is_safe(jpg_path: str) -> bool:
        """Reject a derived preview path that escapes the expected MyWorks/ tree.

        The path comes from the scope's own ``SaveImage.fullname`` event, but we
        never trust it verbatim: it must stay under ``MyWorks/`` with no ``..``
        parent-dir segment, so a crafted event can't make us fetch an arbitrary
        URL on the scope.
        """
        if not jpg_path.startswith(_PREVIEW_PATH_PREFIX):
            return False
        return _PARENT_DIR_SEGMENT not in jpg_path.split("/")

    def _fetch_capped(self, url: str) -> bytes | None:
        """GET ``url`` reading at most :data:`_MAX_PREVIEW_BYTES`.

        Rejects (returns ``None``) when the advertised ``Content-Length`` exceeds
        the cap, and reads one byte past the cap to catch an unbounded/chunked
        body whose length wasn't advertised. Mirrors discovery.py's
        ``_read_capped`` pattern.
        """
        with urllib.request.urlopen(url, timeout=_PREVIEW_TIMEOUT_SEC) as resp:
            content_length = resp.headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > _MAX_PREVIEW_BYTES:
                        _log.warning("%s: preview body %s bytes exceeds cap %d; skipping",
                                     self._device_id, content_length, _MAX_PREVIEW_BYTES)
                        return None
                except ValueError:
                    pass  # bogus header; fall through to the read-side cap
            raw = resp.read(_MAX_PREVIEW_BYTES + 1)
        if len(raw) > _MAX_PREVIEW_BYTES:
            _log.warning("%s: preview body exceeds cap %d (unbounded); skipping",
                         self._device_id, _MAX_PREVIEW_BYTES)
            return None
        return raw

    def _decode_preview(self, raw: bytes) -> bytes | None:
        """Downscale ``raw`` JPEG bytes; never let a bad image kill the worker.

        Returns the re-encoded JPEG, the raw bytes when Pillow is unavailable, or
        ``None`` if decoding fails (a truncated/garbage JPEG, an unidentifiable
        body, or a decompression bomb tripping the pixel ceiling).
        """
        try:
            from PIL import Image, UnidentifiedImageError
        except ModuleNotFoundError:
            return raw  # publish full-size if Pillow is unavailable
        # Module-level pixel ceiling so a decompression-bomb JPEG raises promptly
        # instead of inflating into gigabytes of RAM during decode.
        Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
        max_px = self._settings.preview_max_px
        try:
            img = Image.open(io.BytesIO(raw))
            img.thumbnail((max_px, max_px))
            out = io.BytesIO()
            img.convert("RGB").save(out, format="JPEG", quality=_JPEG_QUALITY)
            return out.getvalue()
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
            _log.warning("%s: preview decode failed; skipping: %s", self._device_id, exc)
            return None

    # -- live /vid camera ---------------------------------------------------------

    def _imaging_stream_url(self) -> str:
        """This scope's live MJPEG stream on seestar_alp's imaging server.

        Derived from the Alpaca base's host (the imaging server always runs
        beside the Alpaca endpoint) plus the ``imaging_port`` setting (default
        7556, seestar_alp's stock bind) and the device number.
        """
        host = urllib.parse.urlsplit(self._settings.alpaca_base).hostname
        return (f"http://{host}:{self._settings.imaging_port}"
                f"/{self._device_num}/{_LIVE_STREAM_PATH}")

    def grab_live_frame(self) -> bytes | None:
        """Read ONE fresh JPEG out of the live MJPEG stream, downscaled.

        Ported from the validated Phase-1 ``grab_preview``: the imaging server
        multiplexes multipart parts each with its own Content-Type — when no
        live frame is available it serves a (large) loading GIF — so the
        boundary is parsed and only a completed ``image/jpeg`` part with real
        JPEG start/end markers is accepted; anything else (the GIF, a
        malformed body) is skipped, never published. Both the wall time and
        the buffered bytes are bounded (:data:`_PREVIEW_TIMEOUT_SEC` /
        :data:`_MAX_PREVIEW_BYTES`) so a slow or bloated stream can neither
        stall the poll loop past the preview timeout nor exhaust memory.
        Returns the downscaled JPEG (via the shared Pillow path), or ``None``
        when no acceptable frame arrived within bounds; transport errors
        propagate to the caller's guard.
        """
        deadline = time.monotonic() + _PREVIEW_TIMEOUT_SEC
        buf = b""
        with urllib.request.urlopen(
                self._imaging_stream_url(), timeout=_PREVIEW_TIMEOUT_SEC) as resp:
            while time.monotonic() < deadline:
                chunk = resp.read(_MJPEG_CHUNK_BYTES)
                if not chunk:
                    break  # stream ended without a usable frame
                buf += chunk
                frame, buf = self._next_jpeg_part(buf)
                if frame is not None:
                    return self._decode_preview(frame)
                if len(buf) > _MAX_PREVIEW_BYTES:
                    _log.warning(
                        "%s: live stream part exceeds cap %d; giving up this cycle",
                        self._device_id, _MAX_PREVIEW_BYTES)
                    break
        return None

    @staticmethod
    def _next_jpeg_part(buf: bytes) -> tuple[bytes | None, bytes]:
        """Scan the completed multipart parts in ``buf`` for the first real JPEG.

        Returns ``(jpeg_body, remaining_buffer)`` when a completed
        ``image/jpeg`` part with valid SOI/EOI markers is present. Otherwise
        returns ``(None, remaining_buffer)`` with every completed non-JPEG
        part (the loading GIF, a mislabeled/truncated body) consumed, so the
        buffer only ever retains the still-incomplete tail of the stream.
        """
        while True:
            start = buf.find(_MJPEG_BOUNDARY)
            header_end = buf.find(_MJPEG_HEADER_END, start) if start >= 0 else -1
            if header_end < 0:
                return None, buf  # part headers not fully buffered yet
            body_start = header_end + len(_MJPEG_HEADER_END)
            next_part = buf.find(_MJPEG_BOUNDARY, body_start)
            if next_part < 0:
                return None, buf  # part body not fully buffered yet
            headers = buf[start:header_end].lower()
            body = buf[body_start:next_part].rstrip(b"\r\n")
            buf = buf[next_part:]
            if (_MJPEG_JPEG_CONTENT_TYPE in headers
                    and body[:len(_JPEG_MAGIC)] == _JPEG_MAGIC
                    and body[-len(_JPEG_END_MAGIC):] == _JPEG_END_MAGIC):
                return body, buf

    def _maybe_publish_live(self, reachable: bool) -> None:
        """Publish one live frame + the live availability for this cycle.

        The live camera is HARD-GATED on session ownership: when this bridge
        does not own the session (or the scope is unreachable this cycle) the
        imaging port is NOT polled at all — a passive observer would only
        receive the Idle placeholder (the firmware boundary; surfaced, not
        worked around) — and the camera's own availability topic reads
        ``offline``. When owned, ONE fresh frame is grabbed per poll cycle; a
        grab failure is logged, marks just this cycle unavailable (ownership
        is kept — the next cycle retries), and leaves the telemetry that was
        already published this cycle untouched.
        """
        if not (reachable and self.session_owned):
            self._publish_live_availability(False)
            return
        try:
            frame = self.grab_live_frame()
        except (*_PROBE_ERRORS, *_pillow_decode_errors()) as exc:
            _log.warning("%s: live frame grab failed; live view offline this cycle: %s",
                         self._device_id, exc)
            frame = None
        if frame:
            self._mqtt.publish(self.live_topic, frame, qos=0, retain=True)
        self._publish_live_availability(frame is not None)

    def _publish_live_availability(self, available: bool) -> None:
        """Publish the live camera's own availability (retained) for this cycle."""
        payload = _PAYLOAD_AVAILABLE if available else _PAYLOAD_NOT_AVAILABLE
        self._mqtt.publish(self.live_availability_topic, payload, retain=True)

    def ensure_site_location(self) -> None:
        """Acquire + cache the scope's GPS site once (for Alt/Az); never published.

        A latitude/longitude of exactly 0 means the GPS has not yet fixed, so we
        leave the cache unset and retry on a later cycle.
        """
        if self._site_lat is not None:
            return
        lat = self._alpaca.get(_SITE_LAT_PROP)
        lon = self._alpaca.get(_SITE_LON_PROP)
        if lat not in _SITE_UNSET_VALUES and lon not in _SITE_UNSET_VALUES:
            self.set_site_location(float(lat), float(lon))

    # -- the poll loop ----------------------------------------------------------

    def run(self) -> None:
        """Poll events fast, device_state slow (with backoff), publish each cycle.

        Runs forever; intended to be the target of a per-scope thread. The loop
        never aborts: a failed event poll is caught in :meth:`_poll_once` (which
        logs the exception and returns an empty state with ``reachable=False``),
        a busy ``get_device_state`` raises ``TimeoutError`` that drives the
        backoff here, and a bad preview is swallowed in :meth:`_maybe_publish_preview`.
        Each cycle publishes the per-scope availability topic ``online`` only when
        the scope is reachable (the event poll succeeded or ``is_connected()``),
        ``offline`` otherwise, and sets the ``connected`` state key the same way.
        The live camera piggybacks on the same cadence: one ``/vid`` frame per
        cycle while the session is owned, its own availability otherwise (see
        :meth:`_maybe_publish_live`).
        """
        last_slow = 0.0
        slow_backoff_until = 0.0
        slow_fail_streak = 0
        last_preview_file = None
        event_poll = self._settings.event_poll_sec
        state_poll = self._settings.state_poll_sec

        while True:
            now = time.time()
            state, saved_fullname, reachable = self._poll_once(now)
            # The connectivity binary_sensor must have a producer: drive it from
            # the scope's Alpaca `connected` property (the event poll alone can't
            # distinguish "scope idle" from "scope disconnected").
            connected = self._alpaca.is_connected()
            state[_CONNECTED_KEY] = connected
            reachable = reachable or connected

            if reachable and now - last_slow >= state_poll and now >= slow_backoff_until:
                last_slow = now
                try:
                    state.update(self.extract_device_state(
                        self._alpaca.action("method_sync", {"method": _DEVICE_STATE_METHOD})))
                    slow_fail_streak = 0
                    slow_backoff_until = 0.0
                except TimeoutError:
                    # A busy scope answers get_device_state with the wait-timeout
                    # sentinel; back off so we don't starve seestar_alp's web UI.
                    slow_fail_streak += 1
                    slow_backoff_until = now + min(
                        _SLOW_BACKOFF_MAX_SEC, state_poll * (_SLOW_BACKOFF_BASE ** slow_fail_streak))
                    _log.info("%s: get_device_state busy; backing off %ds",
                              self._device_id, int(slow_backoff_until - now))
                except RuntimeError as exc:
                    # An Alpaca-level error or an in-band seestar_alp refusal on
                    # the best-effort health probe: skip this cycle's merge and
                    # keep the loop alive (the fields simply stay stale).
                    _log.warning("%s: get_device_state failed; skipping: %s",
                                 self._device_id, exc)
                self._poll_park_flags(state)

            # Publish liveness that reflects reality: 'online' only when the scope
            # actually answered (or reports connected), never a blind 'online'.
            availability = _PAYLOAD_AVAILABLE if reachable else _PAYLOAD_NOT_AVAILABLE
            self._mqtt.publish(self.availability_topic, availability, retain=True)
            # An unreachable scope publishes only the connectivity flag (so the
            # Connected sensor flips OFF), not a stale/empty 'online' snapshot.
            self._mqtt.publish(self.state_topic, json.dumps(state), retain=True)
            last_preview_file = self._maybe_publish_preview(saved_fullname, last_preview_file)
            self._maybe_publish_live(reachable)
            time.sleep(event_poll)

    def _poll_once(self, now: float):
        """One fast event tap: build state, the saved-stack path, and reachability.

        The event tap is the reliable, non-blocking call. A transient I/O failure
        returns ``({}, None, False)`` — an empty state flagged unreachable for
        this cycle — rather than aborting the worker; a success returns the built
        state with ``reachable=True``.
        """
        try:
            event_state = self._alpaca.action(_EVENT_STATE_ACTION, {})
        except (RuntimeError, *_PROBE_ERRORS) as exc:
            # RuntimeError covers an Alpaca-level error or an in-band seestar_alp
            # refusal; either way this cycle degrades to unreachable, not a crash.
            _log.warning("%s: event poll failed: %s", self._device_id, exc)
            return {}, None, False
        state = self.build_state(event_state if isinstance(event_state, dict) else {}, unix_t=now)
        # Confirm the owned-session flag against this same event snapshot (a
        # View in a terminal state means the session ended out from under us).
        self._refresh_session_ownership(event_state)
        self._ensure_site_location_safe()
        if (self._site_lat is not None and state.get("ra") is not None
                and "altitude" not in state):
            # Site arrived after this cycle's extraction; recompute Alt/Az now.
            state["altitude"], state["azimuth"] = radec_to_altaz(
                state["ra"], state["dec"], self._site_lat, self._site_lon, now)
        saved_fullname = _nav(event_state, "SaveImage", "fullname")
        return state, saved_fullname, True

    def _ensure_site_location_safe(self) -> None:
        """Acquire the GPS site, tolerating a transient probe failure (retried)."""
        try:
            self.ensure_site_location()
        except _PROBE_ERRORS as exc:
            _log.info("%s: site location fetch failed (will retry): %s", self._device_id, exc)

    def _poll_park_flags(self, state) -> None:
        """Best-effort park/home flags; a blocked/failed probe skips that flag."""
        for skey, prop in _PARK_PROPS:
            try:
                state[skey] = bool(self._alpaca.get(prop))
            except (TimeoutError, *_PROBE_ERRORS) as exc:
                _log.debug("%s: %s probe skipped: %s", self._device_id, prop, exc)

    def _maybe_publish_preview(self, saved_fullname, last_preview_file):
        """Publish a fresh preview whenever the scope saves a new stacked image.

        A preview fetch/decode failure is non-fatal: log it and keep the prior
        file so the next new save retries. ``fetch_preview`` already swallows
        decode errors internally; the Pillow exceptions are also caught here as a
        backstop so a bad preview degrades to skip-this-cycle rather than killing
        the loop, no matter where the failure surfaces.
        """
        if not saved_fullname or saved_fullname == last_preview_file:
            return last_preview_file
        try:
            frame = self.fetch_preview(saved_fullname)
        except (*_PROBE_ERRORS, *_pillow_decode_errors()) as exc:
            _log.warning("%s: preview fetch failed; skipping this cycle: %s",
                         self._device_id, exc)
            return last_preview_file
        if frame:
            self._mqtt.publish(self.preview_topic, frame, qos=0, retain=True)
            _log.info("%s: published preview (%d bytes) from %s",
                      self._device_id, len(frame), saved_fullname)
            return saved_fullname
        return last_preview_file
