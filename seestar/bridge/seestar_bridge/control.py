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
Param-carrying discrete actions (goto, run-plan, start-live-view) pair a value
entity with a trigger button in ``entities`` but resolve to a single ``Control``
here that reads the value from the command payload.
"""
from __future__ import annotations

import enum
import logging
from collections.abc import Callable, Sequence
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
_METHOD_PI_SHUTDOWN = "pi_shutdown"

# -- imaging modes fed to iscope_start_view (spec's Imaging mode select) ---------
_IMAGING_MODES = ("star", "scenery", "planet", "sun", "moon")

# -- dew-heater power level when the switch is ON. The scope takes a 0..100 value;
#    a non-zero value turns the heater on (action_set_dew_heater keys on > 0). ---
_DEW_HEATER_ON_VALUE = 90
_DEW_HEATER_OFF_VALUE = 0

# -- goto default: a named target with is_j2000 catalog coordinates. goto_target
#    resolves the name via the scope's own catalog, so ra/dec are placeholders. --
_GOTO_PLACEHOLDER_RADEC = 0.0

# -- import_schedule flags: start a fresh run of the imported plan (don't retain
#    the previous scheduler state). --------------------------------------------
_IMPORT_RETAIN_STATE = False

# A single dispatched action as it goes on the wire: the /action name plus the
# Parameters dict the bridge JSON-encodes. A control may emit more than one (e.g.
# run-plan = import_schedule THEN start_scheduler), executed in order.
Call = tuple[str, dict[str, Any]]

#: A control's payload->calls builder. Receives the already-validated payload
#: (a str/number for value entities, or an empty dict for a bare button) and
#: returns the ordered calls to make. Pure: no I/O, no gate logic.
CallBuilder = Callable[[Any], Sequence[Call]]


class Control(NamedTuple):
    """One control entity's declarative spec.

    ``power_gated`` marks the destructive power actions (startup/park/shutdown)
    that additionally require ``allow_power``. ``build`` turns a validated payload
    into the ordered ``(action, params)`` calls. Number controls carry
    ``min_value``/``max_value``/``step``; selects carry ``options`` — both are
    range-checked by :func:`dispatch` before ``build`` ever runs.
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
    """Start the live view in the selected imaging mode (owns the session)."""
    return [(_METHOD_SYNC, {"method": _METHOD_ISCOPE_START_VIEW,
                            "params": {"mode": str(payload)}})]


def _goto(payload: Any) -> Sequence[Call]:
    """Slew to a named target; goto_target resolves the name to coordinates."""
    return [("goto_target", {
        "target_name": str(payload),
        "is_j2000": True,
        "ra": _GOTO_PLACEHOLDER_RADEC,
        "dec": _GOTO_PLACEHOLDER_RADEC,
    })]


def _run_plan(payload: Any) -> Sequence[Call]:
    """Run a saved plan: import it from its filepath, then start the scheduler."""
    return [
        ("import_schedule", {"filepath": str(payload),
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


def _plate_solve_loop(payload: Any) -> Sequence[Call]:
    """Start or stop the polar-align plate-solve loop from a switch."""
    action = "start_plate_solve_loop" if _switch_is_on(payload) else "stop_plate_solve_loop"
    return [(action, {})]


def _park(_payload: Any) -> Sequence[Call]:
    """Stow/shut down the scope (power-gated) via the documented pi_shutdown."""
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
# execution, Power/position). Param-carrying discrete actions use a value entity
# (select/number/text) whose payload the builder reads; the paired trigger button
# in entities.CONTROL_ENTITIES publishes to the SAME command key.
CONTROLS: list[Control] = [
    # -- Session / imaging --
    Control("start_live_view", COMPONENT_SELECT, "Start live view",
            _start_live_view, options=_IMAGING_MODES),
    Control("imaging_mode", COMPONENT_SELECT, "Imaging mode",
            _start_live_view, options=_IMAGING_MODES),
    Control("start_stack", COMPONENT_BUTTON, "Start stacking",
            _simple("start_stack", {"restart": True})),
    Control("stop", COMPONENT_BUTTON, "Stop", _simple("stop_scheduler", {})),
    Control("start_mosaic", COMPONENT_BUTTON, "Start mosaic",
            _simple("start_mosaic", {})),
    Control("start_spectra", COMPONENT_BUTTON, "Start spectra",
            _simple("start_spectra", {})),
    Control("goto", COMPONENT_TEXT, "Goto target", _goto),
    Control("stop_goto", COMPONENT_BUTTON, "Stop goto",
            _simple("stop_goto_target", {})),
    Control("exposure", COMPONENT_NUMBER, "Exposure", _exposure,
            min_value=1, max_value=600, step=1),
    Control("focus", COMPONENT_NUMBER, "Focus", _focus,
            min_value=-500, max_value=500, step=1),
    Control("mag_declination", COMPONENT_NUMBER, "Mag declination",
            _mag_declination, min_value=-180, max_value=180, step=0.1),
    # Keyed distinctly from the Phase-1 read-only ``dew_heater`` binary_sensor so
    # the command switch's unique_id never collides with the sensor's.
    Control("dew_heater_set", COMPONENT_SWITCH, "Dew heater", _dew_heater),
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
    Control("park", COMPONENT_BUTTON, "Park", _park, power_gated=True),
    Control("shutdown", COMPONENT_BUTTON, "Shutdown", _park, power_gated=True),
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
) -> DispatchResult:
    """Safety-gate, validate, and execute one control command.

    The order is deliberate and load-bearing:

    1. **Gate first.** If ``controls_enabled`` is off, refuse every command
       WITHOUT touching ``alpaca``. If the control is power-gated and
       ``allow_power`` is off, refuse WITHOUT touching ``alpaca``.
    2. **Validate.** Unknown ``control_key`` and out-of-range/invalid payloads
       are refused, again without any Alpaca call.
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

    invalid = _validate_payload(control, payload)
    if invalid is not None:
        return _refuse(invalid)

    return _execute(alpaca, control, payload)


def _execute(alpaca: Any, control: Control, payload: Any) -> DispatchResult:
    """Run the control's ordered calls; turn any Alpaca failure into ERROR.

    Kept separate from the gate so the gate path is trivially auditable: this is
    the ONLY place ``alpaca.action`` is invoked, and it is unreachable until the
    gate and validation have both passed.
    """
    calls = control.build(payload)
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
