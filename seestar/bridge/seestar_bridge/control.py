"""Phase-2 control catalog + safety-gated command dispatch.

THIS CODE MOVES A PHYSICAL TELESCOPE. The single most important invariant is the
safety gate in :func:`dispatch`: no command reaches seestar_alp unless the gate
allows it. The gate is checked FIRST, before the control key or its payload are
even validated, and :func:`dispatch` never calls ``alpaca`` on a refusal.

Two per-scope safety switches (both default OFF, published as command entities by
``entities.CONTROL_ENTITIES``) drive the gate:

- ``controls_enabled`` gates **all** commands — until it is on, every incoming
  command is refused with a logged reason so a stray automation can't move the
  scope.
- ``allow_power`` additionally gates the destructive **power** actions
  (startup / park / shutdown). Both switches must be on to power-cycle or stow.

The catalog :data:`CONTROLS` is declarative: each :class:`Control` names the
seestar_alp ``/action`` it invokes (the same non-blocking
``PUT /api/v1/telescope/{n}/action`` the bridge already uses) and a pure
``build`` function that maps the HA payload to the ordered list of
``(action_name, params)`` calls, so the wire contract is auditable in one place.

Param-carrying discrete actions (goto, start-live-view) split into VALUE-ONLY
stored inputs (:data:`STORED_INPUTS` — never dispatched; the worker stores the
value and echoes it to HA) and a trigger button whose ``payload_from_stored``
composes the dispatch payload from those stored values at press time. A builder
may REFUSE by raising ``ValueError`` (missing/unparseable goto coordinates, a
path-traversal plan name); :func:`dispatch` turns that into ``REFUSED`` before
any Alpaca call ever happens.
"""
from __future__ import annotations

import enum
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

_log = logging.getLogger(__name__)

# -- component vocabulary (named, shared with entities.CONTROL_ENTITIES) ---------
COMPONENT_BUTTON = "button"
COMPONENT_SWITCH = "switch"
COMPONENT_SELECT = "select"
COMPONENT_NUMBER = "number"
COMPONENT_TEXT = "text"

# -- HA switch command payloads (paho delivers the raw MQTT string) --------------
_SWITCH_ON = "ON"
_SWITCH_OFF = "OFF"

# -- seestar_alp method_sync method names (actions invoked via the generic
#    method_sync dispatch rather than a dedicated /action name) ------------------
_METHOD_SYNC = "method_sync"
_METHOD_ISCOPE_START_VIEW = "iscope_start_view"
#: The MOUNT-STOW command: seestar_device sends ``{"method": "scope_park"}`` to
#: stow the arm (its shut_down_thread parks with exactly this before powering
#: off). Park uses THIS and only this.
_METHOD_SCOPE_PARK = "scope_park"
#: Powers off the WHOLE device (seestar_device.shut_down_thread: park, then
#: shut down the Pi). Only the ``shutdown`` control may ever send this.
_METHOD_PI_SHUTDOWN = "pi_shutdown"
#: The generic settings RPC the SSC web UI itself uses for gain
#: (front/app.py LiveGainResource: ``set_setting {"isp_gain": <0..300>}``) and
#: the wide-angle camera (LiveWideCamResource: ``set_setting {"wide_cam": bool}``).
_METHOD_SET_SETTING = "set_setting"
#: Mount tracking on/off. Verified wire shape: params is a BARE BOOL —
#: seestar_alp's own Bruno API collection sends
#: ``{"method":"scope_set_track_state", "params":true}`` and its bundled CLI
#: client does the same (cli/ssalp_api_client/commands/mount.py).
_METHOD_SET_TRACK_STATE = "scope_set_track_state"
#: Auto-focus. The upstream firmware RPC really is spelled ``start_auto_focuse``
#: (sic) — verified in seestar_alp device/seestar_device.py:_start_auto_focus
#: and front/app.py's start_auto_focus route, both of which send exactly this.
_METHOD_START_AUTO_FOCUS = "start_auto_focuse"
#: Planetary AVI/MP4 recording, as the SSC web UI's live/video route drives it
#: (front/app.py LiveVideoResource: method_sync start_record_avi with a
#: ``{"raw": bool}`` params dict, method_sync stop_record_avi with none).
_METHOD_START_RECORD_AVI = "start_record_avi"
_METHOD_STOP_RECORD_AVI = "stop_record_avi"

# -- imaging modes fed to iscope_start_view (spec's Imaging mode select) ---------
_IMAGING_MODES = ("star", "scenery", "planet", "sun", "moon")

#: Mode used by 'Start live view' when the operator never touched the
#: Imaging-mode select (matches seestar_alp's own goto_target default).
DEFAULT_IMAGING_MODE = "star"

# -- dew-heater power level when the switch is ON. The scope takes a 0..100 value;
#    a non-zero value turns the heater on (action_set_dew_heater keys on > 0). ---
_DEW_HEATER_ON_VALUE = 90
_DEW_HEATER_OFF_VALUE = 0

# -- stored-input keys: value-only entities the worker stores (never dispatched);
#    trigger buttons compose their dispatch payload from these at press time. ----
IMAGING_MODE_KEY = "imaging_mode"
GOTO_TARGET_KEY = "goto_target"
GOTO_RA_KEY = "goto_ra"
GOTO_DEC_KEY = "goto_dec"

#: Label sent to goto_target when the operator set coordinates but no name.
_GOTO_DEFAULT_NAME = "HA goto"

#: RA is decimal HOURS (0 <= ra < 24) and Dec decimal DEGREES (-90..90), matching
#: seestar_alp's Util.parse_coordinate float branch (ra*u.hour, dec*u.deg).
_RA_HOURS_MAX = 24.0
_DEC_DEG_LIMIT = 90.0

#: Sexagesimal coordinate: "16h41m41s", "16:41:41", "+41d16m9s", "-05:23:28".
#: Degrees/hours then minutes then optional seconds; unit letters or colons.
_SEXAGESIMAL_RE = re.compile(
    r"""^(?P<sign>[+-])?
        (?P<whole>\d{1,3})\s*[hd°:]\s*
        (?P<minutes>\d{1,2}(?:\.\d+)?)\s*(?:[m′':]\s*
        (?P<seconds>\d{1,2}(?:\.\d+)?)\s*[s″"]?)?[m′']?
        $""",
    re.VERBOSE | re.IGNORECASE,
)
_MINUTES_PER_UNIT = 60.0
_SECONDS_PER_UNIT = 3600.0

# -- import_schedule flags: start a fresh run of the imported plan (don't retain
#    the previous scheduler state). --------------------------------------------
_IMPORT_RETAIN_STATE = False

#: The directory (relative to seestar_alp's working directory) where its SSC web
#: UI saves/exports schedules — verified in seestar_alp front/app.py, which does
#: ``os.path.join(os.getcwd(), "schedule")`` for both import and export. Run-plan
#: names are constrained to bare basenames inside this directory because
#: seestar_device.import_schedule does ``open(filepath)`` VERBATIM: an
#: unconstrained value would read any file the driver can.
_PLANS_DIR = "schedule"
_PLAN_SUFFIX = ".json"
_PARENT_DIR = ".."

# -- exposure: action_set_exposure's ``exp`` is MILLISECONDS on the wire
#    (seestar_device: set_setting {"exp_ms": {"stack_l": exp}}). The control is
#    therefore ms end-to-end: solar work needs ~1-5 ms, deep-sky stacking tens of
#    seconds, so the range spans 1 ms .. 60 s with the value passed through. -----
EXPOSURE_MIN_MS = 1
EXPOSURE_MAX_MS = 60_000

# -- gain: the ISP gain range the SSC web UI itself enforces (front/app.py
#    LiveGainResource accepts 0 <= gain <= 300 before set_setting). Mirrored by
#    entities.CONTROL_ENTITIES and the DOCS lock-step test. ----------------------
GAIN_MIN = 0
GAIN_MAX = 300

# A single dispatched action as it goes on the wire: the /action name plus the
# Parameters dict the bridge JSON-encodes. A control may emit more than one (e.g.
# run-plan = import_schedule THEN start_scheduler), executed in order.
Call = tuple[str, dict[str, Any]]

#: A control's payload->calls builder. Receives the already-validated payload
#: (a str/number for value entities, an empty dict for a bare button, or the
#: dict/str composed by ``payload_from_stored``) and returns the ordered calls
#: to make. Pure: no I/O, no gate logic. May raise ``ValueError`` to REFUSE
#: (turned into a REFUSED result by :func:`dispatch`, never an Alpaca call).
CallBuilder = Callable[[Any], Sequence[Call]]

#: Composes a trigger control's dispatch payload from the worker's stored-input
#: values (e.g. the Goto button reads the stored target/RA/Dec). Pure.
StoredComposer = Callable[[Mapping[str, str]], Any]


class Control(NamedTuple):
    """One control entity's declarative spec.

    ``power_gated`` marks the destructive power actions (startup/park/shutdown)
    that additionally require ``allow_power``. ``build`` turns a validated payload
    into the ordered ``(action, params)`` calls. Number controls carry
    ``min_value``/``max_value``/``step``; selects carry ``options`` — both are
    range-checked by :func:`dispatch` before ``build`` ever runs. When
    ``payload_from_stored`` is set, :func:`dispatch` IGNORES the inbound payload
    (a button's ``PRESS``) and composes the real payload from the worker's
    stored-input values instead.
    """

    key: str
    component: str
    name: str
    build: CallBuilder
    power_gated: bool = False
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    options: tuple[str, ...] | None = None
    payload_from_stored: StoredComposer | None = None


class StoredInput(NamedTuple):
    """One value-only input: stored on the worker, echoed to HA, NEVER dispatched.

    A stored input holds a parameter a later trigger button reads (the imaging
    mode for 'Start live view'; the target name/RA/Dec for 'Goto'). Changing one
    must not touch the scope — the worker validates it against ``options`` (when
    set), stores it, and echoes it to the entity's state_topic; only the trigger
    button's dispatch consumes it.
    """

    key: str
    component: str
    options: tuple[str, ...] | None = None


STORED_INPUTS: list[StoredInput] = [
    StoredInput(IMAGING_MODE_KEY, COMPONENT_SELECT, _IMAGING_MODES),
    StoredInput(GOTO_TARGET_KEY, COMPONENT_TEXT),
    StoredInput(GOTO_RA_KEY, COMPONENT_TEXT),
    StoredInput(GOTO_DEC_KEY, COMPONENT_TEXT),
]

_STORED_INPUTS_BY_KEY: dict[str, StoredInput] = {si.key: si for si in STORED_INPUTS}


def stored_input_for(key: str) -> StoredInput | None:
    """Look up a value-only stored input by key, or ``None`` if not one."""
    return _STORED_INPUTS_BY_KEY.get(key)


def validate_stored_input(key: str, payload: Any) -> str | None:
    """Return a refusal reason if ``payload`` is invalid for stored input ``key``.

    A select-backed stored input (imaging_mode) must be one of its options; a
    free-text one (goto target/RA/Dec) accepts any string — the goto builder
    parses and range-checks it at dispatch time, where a bad value REFUSES the
    slew rather than being stored wrong silently.
    """
    stored = _STORED_INPUTS_BY_KEY.get(key)
    if stored is None:
        return f"unknown stored input {key!r}"
    if stored.options is not None and str(payload) not in stored.options:
        return f"{key}: {payload!r} not in options {list(stored.options)}"
    return None


class DispatchStatus(enum.Enum):
    """Outcome of a :func:`dispatch` call.

    ``OK`` — the command was validated, gated-through, and handed to Alpaca.
    ``REFUSED`` — the gate blocked it or validation failed; Alpaca was NOT called.
    ``ERROR`` — Alpaca was called but the ``/action`` raised (scope busy, RPC
    timeout, transport error); surfaced so the caller can notify, never swallowed.
    """

    OK = "ok"
    REFUSED = "refused"
    ERROR = "error"


@dataclass(frozen=True)
class DispatchResult:
    """Typed result of a dispatch: a status plus a human-readable reason.

    ``reason`` is always populated for REFUSED/ERROR (it is what gets logged and
    surfaced to the operator) and empty for a clean OK.
    """

    status: DispatchStatus
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status is DispatchStatus.OK


# -- payload -> calls builders (pure) --------------------------------------------


def _simple(action_name: str, params: dict[str, Any] | None = None) -> CallBuilder:
    """A builder that always emits one fixed ``(action_name, params)`` call.

    Used for buttons whose action takes no payload-derived parameters.
    """
    fixed = params or {}
    return lambda _payload: [(action_name, dict(fixed))]


def _start_live_view(payload: Any) -> Sequence[Call]:
    """Start the live view in the stored imaging mode (owns the session).

    The payload is composed from the stored ``imaging_mode`` (default
    :data:`DEFAULT_IMAGING_MODE`); re-checked here as defense in depth even
    though the worker only stores validated modes.
    """
    mode = str(payload)
    if mode not in _IMAGING_MODES:
        raise ValueError(f"imaging mode {mode!r} not in {list(_IMAGING_MODES)}")
    return [(_METHOD_SYNC, {"method": _METHOD_ISCOPE_START_VIEW,
                            "params": {"mode": mode}})]


def _parse_angle(raw: Any, *, label: str) -> float:
    """Parse a decimal or sexagesimal coordinate string to a float, or refuse.

    Accepts ``"10.6847"`` (decimal), ``"0h42m44s"`` / ``"0:42:44"`` (RA), and
    ``"+41d16m9s"`` / ``"-05:23:28"`` (Dec). Raises ``ValueError`` with an
    operator-readable reason when missing or unparseable — NEVER substitutes a
    default: a fabricated coordinate would slew the scope somewhere real.
    """
    text = str(raw or "").strip()
    if not text:
        raise ValueError(f"{label} is not set")
    try:
        return float(text)
    except ValueError:
        pass
    match = _SEXAGESIMAL_RE.match(text)
    if match is None:
        raise ValueError(
            f"{label} {text!r} is neither decimal nor sexagesimal (e.g. 0h42m44s)")
    value = (float(match.group("whole"))
             + float(match.group("minutes")) / _MINUTES_PER_UNIT
             + float(match.group("seconds") or 0.0) / _SECONDS_PER_UNIT)
    return -value if match.group("sign") == "-" else value


def _goto(payload: Any) -> Sequence[Call]:
    """Slew to REAL coordinates composed from the stored goto inputs.

    seestar_alp's ``goto_target`` does NOT resolve names — it feeds ``ra``/``dec``
    straight into Util.parse_coordinate (float => J2000 hours/degrees), so this
    builder REFUSES (ValueError, no Alpaca call) unless the operator provided
    parseable, in-range coordinates. ``target_name`` is only the session label.
    """
    stored = payload if isinstance(payload, dict) else {}
    ra = _parse_angle(stored.get("ra"), label="goto RA")
    dec = _parse_angle(stored.get("dec"), label="goto Dec")
    if not 0.0 <= ra < _RA_HOURS_MAX:
        raise ValueError(f"goto RA {ra} outside 0..{_RA_HOURS_MAX} hours")
    if not -_DEC_DEG_LIMIT <= dec <= _DEC_DEG_LIMIT:
        raise ValueError(f"goto Dec {dec} outside ±{_DEC_DEG_LIMIT} degrees")
    target_name = str(stored.get("target_name") or "").strip() or _GOTO_DEFAULT_NAME
    return [("goto_target", {
        "target_name": target_name,
        "is_j2000": True,
        "ra": ra,
        "dec": dec,
    })]


def _run_plan(payload: Any) -> Sequence[Call]:
    """Run a saved plan by NAME: import it from the plans dir, then start.

    seestar_alp's ``import_schedule`` does ``open(filepath)`` verbatim, so the
    name is strictly constrained to a bare basename (no path separators, no
    ``..``, not absolute) inside its ``schedule/`` directory — the same place its
    own SSC web UI saves plans. A ``.json`` suffix is appended when omitted.
    Anything else REFUSES (ValueError) before any Alpaca call.
    """
    name = str(payload or "").strip()
    if not name:
        raise ValueError("run_plan: no plan name given")
    if ("/" in name or "\\" in name or _PARENT_DIR in name
            or name.startswith(("~", "."))):
        raise ValueError(
            f"run_plan: {name!r} is not a plan name (bare filename in the scope's "
            f"{_PLANS_DIR}/ directory; no paths)")
    if not name.endswith(_PLAN_SUFFIX):
        name += _PLAN_SUFFIX
    return [
        ("import_schedule", {"filepath": f"{_PLANS_DIR}/{name}",
                             "is_retain_state": _IMPORT_RETAIN_STATE}),
        ("start_scheduler", {}),
    ]


def _exposure(payload: Any) -> Sequence[Call]:
    """Set the stack exposure (ms) and recreate the dark frame."""
    return [("action_set_exposure", {"exp": _as_number(payload)})]


def _focus(payload: Any) -> Sequence[Call]:
    """Nudge the focuser by a relative number of steps."""
    return [("adjust_focus", {"steps": _as_number(payload)})]


def _mag_declination(payload: Any) -> Sequence[Call]:
    """Apply a magnetic-declination fudge angle to the compass calibration."""
    return [("adjust_mag_declination",
             {"adjust_mag_dec": True, "fudge_angle": _as_number(payload)})]


def _dew_heater(payload: Any) -> Sequence[Call]:
    """Turn the dew heater on (a non-zero power level) or off."""
    value = _DEW_HEATER_ON_VALUE if _switch_is_on(payload) else _DEW_HEATER_OFF_VALUE
    return [("action_set_dew_heater", {"heater": value})]


def _gain(payload: Any) -> Sequence[Call]:
    """Set the ISP gain, exactly as the SSC web UI's live/gain route does."""
    return [(_METHOD_SYNC, {"method": _METHOD_SET_SETTING,
                            "params": {"isp_gain": _as_number(payload)}})]


def _tracking(payload: Any) -> Sequence[Call]:
    """Turn mount tracking on/off (scope_set_track_state, bare-bool params)."""
    return [(_METHOD_SYNC, {"method": _METHOD_SET_TRACK_STATE,
                            "params": _switch_is_on(payload)})]


def _wide_cam(payload: Any) -> Sequence[Call]:
    """Enable/disable the wide-angle camera (S30-series), via set_setting."""
    return [(_METHOD_SYNC, {"method": _METHOD_SET_SETTING,
                            "params": {"wide_cam": _switch_is_on(payload)}})]


def _auto_focus(_payload: Any) -> Sequence[Call]:
    """Run the auto-focus routine (upstream RPC spelled start_auto_focuse)."""
    return [(_METHOD_SYNC, {"method": _METHOD_START_AUTO_FOCUS})]


def _record_video(payload: Any) -> Sequence[Call]:
    """Start/stop planetary AVI recording, as the SSC live/video route does.

    ON starts a plain (non-raw, non-timelapse) recording — the web UI's own
    default form post; OFF stops it. Only meaningful during a planetary live
    session; outside one the firmware refuses in-band, which surfaces as an
    ERROR dispatch result on the command-result sensor.
    """
    if _switch_is_on(payload):
        return [(_METHOD_SYNC, {"method": _METHOD_START_RECORD_AVI,
                                "params": {"raw": False}})]
    return [(_METHOD_SYNC, {"method": _METHOD_STOP_RECORD_AVI})]


def _plate_solve_loop(payload: Any) -> Sequence[Call]:
    """Start or stop the polar-align plate-solve loop from a switch."""
    action = "start_plate_solve_loop" if _switch_is_on(payload) else "stop_plate_solve_loop"
    return [(action, {})]


def _park(_payload: Any) -> Sequence[Call]:
    """Stow the MOUNT ONLY via scope_park (power-gated); never powers off.

    Verified against seestar_alp: ``scope_park`` is the raw mount-stow RPC its
    own shut_down_thread issues before a power-off; the ASCOM ``PUT .../park``
    endpoint is a no-op stub, so method_sync is the working transport for it.
    Park MUST NOT share a builder with shutdown — ``pi_shutdown`` powers off the
    entire device (park + Pi halt).
    """
    return [(_METHOD_SYNC, {"method": _METHOD_SCOPE_PARK})]


def _shutdown(_payload: Any) -> Sequence[Call]:
    """Power off the WHOLE device (power-gated): seestar_alp parks, then halts."""
    return [(_METHOD_SYNC, {"method": _METHOD_PI_SHUTDOWN})]


def _switch_is_on(payload: Any) -> bool:
    """Interpret an HA switch command payload (``"ON"``/``"OFF"``) as a bool."""
    return str(payload).strip().upper() == _SWITCH_ON


def _as_number(payload: Any) -> float | int:
    """Coerce a validated number payload to int when whole, else float.

    Dispatch has already range-checked the value, so this only normalises the
    type (an int step reads cleaner on the wire than ``30.0``).
    """
    value = float(payload)
    return int(value) if value.is_integer() else value


# -- the catalog -----------------------------------------------------------------
#
# Full 'Control entity catalog' from the Phase-2 spec (Session/imaging, Plans
# execution, Power/position). Param-carrying discrete actions pair value-only
# STORED_INPUTS (imaging_mode, goto_target/goto_ra/goto_dec — stored + echoed by
# the worker, never dispatched) with a trigger button here whose
# ``payload_from_stored`` composes the real payload at press time.
#
# KNOWN ROUGH EDGES (verified against seestar_alp device/telescope.py +
# device/seestar_device.py):
# - 'Stop' maps to stop_scheduler, so it only stops a SCHEDULER-driven session;
#   a live view / stack started outside the scheduler is not stopped by it.
# - 'Plate-solve loop' ON is a firmware > 2.47 no-op: seestar_alp answers
#   start_plate_solve_loop with a "Deprecated" warning and does nothing (OFF
#   still calls stop_plate_solve_loop).
# - 'Wide camera' is S30-series only (upstream's own UI additionally hides it
#   behind Config.experimental); other models refuse/ignore the setting.
# - 'Record video' only records during a planetary live session (the firmware
#   refuses start_record_avi otherwise, in-band -> an ERROR dispatch result).
# - seestar_alp reports many refusals IN-BAND: HTTP 200 with a json_result body
#   of ``{"code": -1, "result": "..."}`` (e.g. import_schedule while a scheduler
#   is active, action_start_up_sequence while busy). Alpaca.action detects that
#   shape and raises, so these surface as ERROR dispatch results, not silent OKs.
CONTROLS: list[Control] = [
    # -- Session / imaging --
    Control("start_live_view", COMPONENT_BUTTON, "Start live view",
            _start_live_view,
            payload_from_stored=lambda stored: stored.get(
                IMAGING_MODE_KEY, DEFAULT_IMAGING_MODE)),
    Control("start_stack", COMPONENT_BUTTON, "Start stacking",
            _simple("start_stack", {"restart": True})),
    Control("stop", COMPONENT_BUTTON, "Stop", _simple("stop_scheduler", {})),
    Control("start_mosaic", COMPONENT_BUTTON, "Start mosaic",
            _simple("start_mosaic", {})),
    Control("start_spectra", COMPONENT_BUTTON, "Start spectra",
            _simple("start_spectra", {})),
    # Goto is a TRIGGER: it dispatches with the stored target name + RA/Dec text
    # inputs and REFUSES unless the coordinates parse (goto_target does not
    # resolve names, so fabricating ra/dec would slew to a wrong, real place).
    Control("goto", COMPONENT_BUTTON, "Goto", _goto,
            payload_from_stored=lambda stored: {
                "target_name": stored.get(GOTO_TARGET_KEY),
                "ra": stored.get(GOTO_RA_KEY),
                "dec": stored.get(GOTO_DEC_KEY),
            }),
    Control("stop_goto", COMPONENT_BUTTON, "Stop goto",
            _simple("stop_goto_target", {})),
    # Exposure is MILLISECONDS end-to-end (action_set_exposure's ``exp`` is ms).
    Control("exposure", COMPONENT_NUMBER, "Stack exposure", _exposure,
            min_value=EXPOSURE_MIN_MS, max_value=EXPOSURE_MAX_MS, step=1),
    # Gain mirrors the SSC web UI's own live/gain route: set_setting isp_gain,
    # range-checked to the UI's 0..300.
    Control("gain", COMPONENT_NUMBER, "Gain", _gain,
            min_value=GAIN_MIN, max_value=GAIN_MAX, step=1),
    Control("focus", COMPONENT_NUMBER, "Focus", _focus,
            min_value=-500, max_value=500, step=1),
    Control("auto_focus", COMPONENT_BUTTON, "Auto-focus", _auto_focus),
    Control("mag_declination", COMPONENT_NUMBER, "Mag declination",
            _mag_declination, min_value=-180, max_value=180, step=0.1),
    # Keyed distinctly from the Phase-1 read-only ``tracking`` binary_sensor so
    # the command switch's unique_id never collides with the sensor's (the same
    # convention as dew_heater_set below).
    Control("tracking_set", COMPONENT_SWITCH, "Tracking", _tracking),
    # Keyed distinctly from the Phase-1 read-only ``dew_heater`` binary_sensor so
    # the command switch's unique_id never collides with the sensor's.
    Control("dew_heater_set", COMPONENT_SWITCH, "Dew heater", _dew_heater),
    Control("wide_cam", COMPONENT_SWITCH, "Wide camera", _wide_cam),
    Control("record_video", COMPONENT_SWITCH, "Record video", _record_video),
    Control("plate_solve_loop", COMPONENT_SWITCH, "Plate-solve loop",
            _plate_solve_loop),
    # -- Plans (execution only) --
    Control("run_plan", COMPONENT_TEXT, "Run plan", _run_plan),
    Control("pause_plan", COMPONENT_BUTTON, "Pause plan",
            _simple("pause_scheduler", {})),
    Control("continue_plan", COMPONENT_BUTTON, "Continue plan",
            _simple("continue_scheduler", {})),
    Control("skip_target", COMPONENT_BUTTON, "Skip current target",
            _simple("skip_scheduler_cur_item", {})),
    Control("reset_item", COMPONENT_BUTTON, "Reset current item",
            _simple("reset_scheduler_cur_item", {})),
    # -- Power / position (behind allow_power) --
    Control("startup", COMPONENT_BUTTON, "Startup sequence",
            _simple("action_start_up_sequence", {}), power_gated=True),
    # Park stows the MOUNT ONLY (scope_park); Shutdown powers off the WHOLE
    # device (pi_shutdown). They deliberately have DIFFERENT builders.
    Control("park", COMPONENT_BUTTON, "Park", _park, power_gated=True),
    Control("shutdown", COMPONENT_BUTTON, "Shutdown", _shutdown, power_gated=True),
]

_CONTROLS_BY_KEY: dict[str, Control] = {ctl.key: ctl for ctl in CONTROLS}


def control_for(control_key: str) -> Control | None:
    """Look up a dispatch-catalog :class:`Control` by key, or ``None`` if unknown.

    Public accessor over the by-key index so the scope worker can ask whether a
    dispatched control is stateful (to decide whether to echo its value to HA)
    without reaching into module internals or re-scanning :data:`CONTROLS`.
    """
    return _CONTROLS_BY_KEY.get(control_key)


# -- validation ------------------------------------------------------------------


def _validate_payload(control: Control, payload: Any) -> str | None:
    """Return a refusal reason if ``payload`` is out of range, else ``None``.

    Number controls are bounds-checked against min/max; selects against their
    option set. Buttons/text/switch take any payload (a button ignores it; text
    is free-form; a switch is ON/OFF). A non-numeric payload for a number control
    is itself a refusal (never coerced blindly into a scope command).
    """
    if control.component == COMPONENT_NUMBER:
        try:
            value = float(payload)
        except (TypeError, ValueError):
            return f"{control.key}: payload {payload!r} is not a number"
        if control.min_value is not None and value < control.min_value:
            return f"{control.key}: {value} below minimum {control.min_value}"
        if control.max_value is not None and value > control.max_value:
            return f"{control.key}: {value} above maximum {control.max_value}"
    elif control.component == COMPONENT_SELECT:
        if control.options is not None and str(payload) not in control.options:
            return f"{control.key}: {payload!r} not in options {list(control.options)}"
    return None


# -- dispatch --------------------------------------------------------------------


def dispatch(
    alpaca: Any,
    control_key: str,
    payload: Any,
    *,
    controls_enabled: bool,
    allow_power: bool,
    stored: Mapping[str, str] | None = None,
) -> DispatchResult:
    """Safety-gate, validate, and execute one control command.

    ``stored`` is the worker's read-only stored-input snapshot (imaging mode,
    goto target/RA/Dec); a control with ``payload_from_stored`` composes its
    real payload from it, ignoring the inbound trigger payload.

    The order is deliberate and load-bearing:

    1. **Gate first.** If ``controls_enabled`` is off, refuse every command
       WITHOUT touching ``alpaca``. If the control is power-gated and
       ``allow_power`` is off, refuse WITHOUT touching ``alpaca``.
    2. **Validate.** Unknown ``control_key`` and out-of-range/invalid payloads
       are refused, again without any Alpaca call — including a builder raising
       ``ValueError`` (missing goto coordinates, a traversal plan name).
    3. **Execute.** Only then are the control's ``(action, params)`` calls made
       via ``alpaca.action`` (the ``PUT /action`` transport), in order. An Alpaca
       exception becomes an ``ERROR`` result (logged), never a raised exception.

    Returns a :class:`DispatchResult`; on REFUSED/ERROR the reason is logged so a
    dropped command is never silent.
    """
    if not controls_enabled:
        return _refuse(
            f"{control_key}: controls are disabled (arm 'Controls enabled' first)")

    control = _CONTROLS_BY_KEY.get(control_key)
    if control is None:
        return _refuse(f"unknown control key {control_key!r}")

    if control.power_gated and not allow_power:
        return _refuse(
            f"{control_key}: power actions are disabled (arm 'Allow power actions')")

    if control.payload_from_stored is not None:
        payload = control.payload_from_stored(stored or {})

    invalid = _validate_payload(control, payload)
    if invalid is not None:
        return _refuse(invalid)

    # Build BEFORE any Alpaca contact: a builder ValueError is a refusal (bad or
    # missing inputs), and by construction nothing has reached the scope yet.
    try:
        calls = control.build(payload)
    except ValueError as exc:
        return _refuse(f"{control.key}: {exc}")

    return _execute(alpaca, control, calls)


def _execute(alpaca: Any, control: Control, calls: Sequence[Call]) -> DispatchResult:
    """Run the control's ordered calls; turn any Alpaca failure into ERROR.

    Kept separate from the gate so the gate path is trivially auditable: this is
    the ONLY place ``alpaca.action`` is invoked, and it is unreachable until the
    gate, validation, and the builder have all passed.
    """
    try:
        for action_name, params in calls:
            alpaca.action(action_name, params)
    except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
        reason = f"{control.key}: {action_name} failed: {exc}"
        _log.warning("control dispatch error: %s", reason)
        return DispatchResult(DispatchStatus.ERROR, reason)
    _log.info("control dispatched: %s -> %s", control.key,
              [name for name, _ in calls])
    return DispatchResult(DispatchStatus.OK)


def _refuse(reason: str) -> DispatchResult:
    """Build a REFUSED result and log the reason (a refusal is never silent)."""
    _log.warning("control refused: %s", reason)
    return DispatchResult(DispatchStatus.REFUSED, reason)
