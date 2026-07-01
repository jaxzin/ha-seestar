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
and publish the per-scope availability ('online' only when the scope is actually
reachable) + the state JSON each cycle.
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
from .entities import (
    CONTROL_ENTITIES,
    ENTITIES,
    availability_list,
    control_discovery_payload,
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

# --- HTTP + backoff constants --------------------------------------------------

#: The scope writes both a .fit and a viewable .jpg under MyWorks/; we fetch the
#: .jpg sibling of the saved .fit path.
_FIT_SUFFIX = ".fit"
_JPG_SUFFIX = ".jpg"
_JPEG_MAGIC = b"\xff\xd8"

_PREVIEW_TIMEOUT_SEC = 30
_JPEG_QUALITY = 85

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
            payload = discovery_payload(
                entity,
                device_block=block,
                base_topic=self.base_topic,
                bridge_availability_topic=self._bridge_availability_topic,
            )
            self._mqtt.publish(topic, json.dumps(payload), retain=True)
        # Phase-2 command entities: same per-scope device, each with a
        # command_topic the bridge subscribes to (subscription is wired in run()).
        for control in CONTROL_ENTITIES:
            topic = f"{prefix}/{control.component}/{self._device_id}/{control.key}/config"
            payload = control_discovery_payload(
                control,
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
                self._poll_park_flags(state)

            # Publish liveness that reflects reality: 'online' only when the scope
            # actually answered (or reports connected), never a blind 'online'.
            availability = _PAYLOAD_AVAILABLE if reachable else _PAYLOAD_NOT_AVAILABLE
            self._mqtt.publish(self.availability_topic, availability, retain=True)
            # An unreachable scope publishes only the connectivity flag (so the
            # Connected sensor flips OFF), not a stale/empty 'online' snapshot.
            self._mqtt.publish(self.state_topic, json.dumps(state), retain=True)
            last_preview_file = self._maybe_publish_preview(saved_fullname, last_preview_file)
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
        except _PROBE_ERRORS as exc:
            _log.warning("%s: event poll failed: %s", self._device_id, exc)
            return {}, None, False
        state = self.build_state(event_state if isinstance(event_state, dict) else {}, unix_t=now)
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
