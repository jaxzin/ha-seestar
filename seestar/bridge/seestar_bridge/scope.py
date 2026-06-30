"""Per-scope worker: pure state extraction + the live poll/publish loop.

One :class:`ScopeWorker` owns one telescope. :meth:`ScopeWorker.build_state` is
PURE — it maps a seestar_alp ``get_event_state`` dict to the entity-key state
dict whose keys exactly match :data:`seestar_bridge.entities.ENTITIES` — and is
deliberately importable without paho or Pillow (the MQTT factory lives in
``mqtt.py``; Pillow is lazy-imported inside the preview fetch). Everything that
was a module-global in the validated Phase-1 bridge (device id/name, base topic,
the cached GPS site) is now per-instance, so a single process can drive a
distinct HA device per scope.

:meth:`ScopeWorker.run` is the loop: tap the non-blocking event stream every
``event_poll_sec``; probe ``get_device_state`` on a slow cadence with the
Phase-1 exponential backoff (it only answers when the scope is briefly idle);
fetch + downscale the saved stack preview whenever a new ``SaveImage`` lands;
and publish availability + the state JSON each cycle.
"""
from __future__ import annotations

import io
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .altaz import radec_to_altaz
from .entities import ENTITIES, device_block, discovery_payload, slug

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

_CAMERA_COMPONENT = "camera"
_CAMERA_KEY = "preview"
_CAMERA_NAME = "Live stacked preview"

# --- HTTP + backoff constants --------------------------------------------------

#: The scope writes both a .fit and a viewable .jpg under MyWorks/; we fetch the
#: .jpg sibling of the saved .fit path.
_FIT_SUFFIX = ".fit"
_JPG_SUFFIX = ".jpg"
_JPEG_MAGIC = b"\xff\xd8"

_PREVIEW_TIMEOUT_SEC = 30
_JPEG_QUALITY = 85

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

    def __init__(self, alpaca, device, settings, mqtt_client, scope_http_base, *, device_id=None):
        self._alpaca = alpaca
        self._device = device
        self._settings = settings
        self._mqtt = mqtt_client
        self._scope_http_base = scope_http_base.rstrip("/") if scope_http_base else None
        # Stable HA device id = slug of the scope name (caller may override to
        # disambiguate a name collision by device_num).
        self._device_id = device_id or slug(device.get("DeviceName", ""))
        self._device_name = device.get("DeviceName", self._device_id)
        # Observer location from the scope's own GPS; cached once, never published.
        self._site_lat: float | None = None
        self._site_lon: float | None = None

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
            payload = discovery_payload(entity, device_block=block, base_topic=self.base_topic)
            self._mqtt.publish(topic, json.dumps(payload), retain=True)
        camera = {
            "name": _CAMERA_NAME,
            "unique_id": f"{self._device_id}_{_CAMERA_KEY}",
            "object_id": f"{self._device_id}_{_CAMERA_KEY}",
            "topic": self.preview_topic,
            "availability_topic": self.availability_topic,
            "payload_available": _PAYLOAD_AVAILABLE,
            "payload_not_available": _PAYLOAD_NOT_AVAILABLE,
            "device": block,
        }
        camera_topic = f"{prefix}/{_CAMERA_COMPONENT}/{self._device_id}/{_CAMERA_KEY}/config"
        self._mqtt.publish(camera_topic, json.dumps(camera), retain=True)

    def fetch_preview(self, fullname: str) -> bytes | None:
        """Fetch the saved stacked .jpg from the scope's HTTP server, downscaled.

        Pillow is lazy-imported here so ``build_state`` stays importable without
        it. Returns JPEG bytes, or ``None`` if no scope address is known or the
        body is not a JPEG. Publishes full-size if Pillow is unavailable.
        """
        if not self._scope_http_base:
            return None
        jpg_path = fullname.rsplit(".", 1)[0] + _JPG_SUFFIX
        url = f"{self._scope_http_base}/{urllib.parse.quote(jpg_path)}"
        with urllib.request.urlopen(url, timeout=_PREVIEW_TIMEOUT_SEC) as resp:
            raw = resp.read()
        if raw[:len(_JPEG_MAGIC)] != _JPEG_MAGIC:
            return None
        try:
            from PIL import Image
        except ModuleNotFoundError:
            return raw  # publish full-size if Pillow is unavailable
        max_px = self._settings.preview_max_px
        img = Image.open(io.BytesIO(raw))
        img.thumbnail((max_px, max_px))
        out = io.BytesIO()
        img.convert("RGB").save(out, format="JPEG", quality=_JPEG_QUALITY)
        return out.getvalue()

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

        Runs forever; intended to be the target of a per-scope thread. Event
        failures and best-effort device_state timeouts are logged via the
        loop's accounting and never abort the loop.
        """
        last_slow = 0.0
        slow_backoff_until = 0.0
        slow_fail_streak = 0
        last_preview_file = None
        event_poll = self._settings.event_poll_sec
        state_poll = self._settings.state_poll_sec

        while True:
            now = time.time()
            state, saved_fullname = self._poll_once(now)

            if now - last_slow >= state_poll and now >= slow_backoff_until:
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
                self._poll_park_flags(state)

            self._mqtt.publish(self.availability_topic, _PAYLOAD_AVAILABLE, retain=True)
            self._mqtt.publish(self.state_topic, json.dumps(state), retain=True)
            last_preview_file = self._maybe_publish_preview(saved_fullname, last_preview_file)
            time.sleep(event_poll)

    def _poll_once(self, now: float):
        """One fast event tap: build state + return the saved-stack path (if any).

        The event tap is the reliable, non-blocking call; a transient I/O failure
        publishes an empty state for this cycle rather than aborting the worker.
        """
        try:
            event_state = self._alpaca.action(_EVENT_STATE_ACTION, {})
        except _PROBE_ERRORS as exc:
            _log.warning("%s: event poll failed: %s", self._device_id, exc)
            return {}, None
        state = self.build_state(event_state if isinstance(event_state, dict) else {}, unix_t=now)
        self._ensure_site_location_safe()
        if (self._site_lat is not None and state.get("ra") is not None
                and "altitude" not in state):
            # Site arrived after this cycle's extraction; recompute Alt/Az now.
            state["altitude"], state["azimuth"] = radec_to_altaz(
                state["ra"], state["dec"], self._site_lat, self._site_lon, now)
        saved_fullname = _nav(event_state, "SaveImage", "fullname")
        return state, saved_fullname

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

        A preview fetch failure is non-fatal: log it and keep the prior file so
        the next new save retries.
        """
        if not saved_fullname or saved_fullname == last_preview_file:
            return last_preview_file
        try:
            frame = self.fetch_preview(saved_fullname)
        except _PROBE_ERRORS as exc:
            _log.warning("%s: preview fetch failed: %s", self._device_id, exc)
            return last_preview_file
        if frame:
            self._mqtt.publish(self.preview_topic, frame, qos=0, retain=True)
            _log.info("%s: published preview (%d bytes) from %s",
                      self._device_id, len(frame), saved_fullname)
            return saved_fullname
        return last_preview_file
