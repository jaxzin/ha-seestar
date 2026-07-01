"""Tests for the Phase-2 control catalog + safety-gated dispatch (control.py)
and the command-entity discovery path (entities.CONTROL_ENTITIES).

TDD contract for a module that MOVES A PHYSICAL TELESCOPE, so the gate tests
come first and assert the strongest property: when gated, ``dispatch`` must
NEVER touch Alpaca. The stub :class:`_StubAlpaca` records every ``action`` call
so a single ``calls == []`` assertion proves no command reached the scope.

Groups:
- gate defaults: both safety switches OFF => every command refused, no Alpaca call
- controls_enabled: arms non-power commands; power still needs allow_power too
- validation: unknown key refused; out-of-range number refused; both before Alpaca
- mappings: representative controls translate to the exact (action, params)
- discovery: a button gets a command_topic + no state; a switch gets both
"""
from __future__ import annotations

import pytest

from seestar_bridge import control
from seestar_bridge.control import DispatchStatus, dispatch
from seestar_bridge.entities import (
    CONTROL_ENTITIES,
    control_discovery_payload,
    device_block,
)

DEVICE_ID = "seestar_s30_pro"
DEVICE_NAME = "Seestar S30 Pro"
BASE_TOPIC = f"seestar/{DEVICE_ID}"
BRIDGE_AVAILABILITY_TOPIC = "seestar/bridge/availability"


class _StubAlpaca:
    """Records every ``action`` call; never performs I/O.

    ``calls`` is the audit trail the gate tests assert against: an empty list
    proves ``dispatch`` refused a command WITHOUT reaching the scope.
    """

    def __init__(self, result="ok", raise_exc=None):
        self.calls: list[tuple[str, dict]] = []
        self._result = result
        self._raise = raise_exc

    def action(self, name, params=None):
        self.calls.append((name, params or {}))
        if self._raise is not None:
            raise self._raise
        return self._result


def _control(key):
    """Find the control-catalog entry by key (tests address by key, not index)."""
    for ctl in control.CONTROLS:
        if ctl.key == key:
            return ctl
    raise KeyError(key)


# -- gate defaults ----------------------------------------------------------------

def test_gate_default_off_refuses_and_never_calls_alpaca():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "start_live_view", {}, controls_enabled=False, allow_power=False)
    assert result.status is DispatchStatus.REFUSED
    assert result.reason  # a human-readable reason is always attached
    assert alpaca.calls == []  # CRITICAL: nothing reached the scope


def test_gate_default_off_refuses_power_action_too():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "park", {}, controls_enabled=False, allow_power=False)
    assert result.status is DispatchStatus.REFUSED
    assert alpaca.calls == []


# -- controls_enabled arms non-power commands -------------------------------------

def test_controls_enabled_allows_non_power_command():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "start_stack", {}, controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    assert len(alpaca.calls) == 1


def test_power_action_needs_controls_enabled_and_allow_power():
    # controls_enabled but NOT allow_power => power action refused, no Alpaca call.
    alpaca = _StubAlpaca()
    refused = dispatch(
        alpaca, "park", {}, controls_enabled=True, allow_power=False)
    assert refused.status is DispatchStatus.REFUSED
    assert alpaca.calls == []

    # both switches ON => the power action is dispatched.
    alpaca2 = _StubAlpaca()
    allowed = dispatch(
        alpaca2, "park", {}, controls_enabled=True, allow_power=True)
    assert allowed.status is DispatchStatus.OK
    assert len(alpaca2.calls) == 1


def test_allow_power_alone_does_not_arm_anything():
    # allow_power without controls_enabled must still refuse (controls_enabled
    # gates ALL commands, power or not).
    alpaca = _StubAlpaca()
    refused_power = dispatch(
        alpaca, "park", {}, controls_enabled=False, allow_power=True)
    assert refused_power.status is DispatchStatus.REFUSED
    refused_plain = dispatch(
        alpaca, "start_stack", {}, controls_enabled=False, allow_power=True)
    assert refused_plain.status is DispatchStatus.REFUSED
    assert alpaca.calls == []


# -- validation -------------------------------------------------------------------

def test_unknown_control_key_refused_without_alpaca_call():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "not_a_real_control", {}, controls_enabled=True, allow_power=True)
    assert result.status is DispatchStatus.REFUSED
    assert alpaca.calls == []


def test_number_out_of_range_refused_without_alpaca_call():
    # Exposure has a max; a value above it must be refused before any Alpaca call.
    exposure = _control("exposure")
    over = exposure.max_value + 1
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "exposure", over, controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.REFUSED
    assert alpaca.calls == []


def test_number_below_min_refused():
    exposure = _control("exposure")
    under = exposure.min_value - 1
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "exposure", under, controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.REFUSED
    assert alpaca.calls == []


def test_number_in_range_accepted():
    exposure = _control("exposure")
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "exposure", exposure.min_value, controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    assert len(alpaca.calls) == 1


def test_stored_input_rejects_value_outside_options():
    # imaging_mode is a value-only stored input; its select options are enforced
    # by validate_stored_input (the worker refuses to store an unknown mode).
    assert control.validate_stored_input("imaging_mode", "not_a_mode") is not None
    assert control.validate_stored_input("imaging_mode", "moon") is None


def test_stored_input_key_is_not_dispatchable():
    # A stored input (imaging_mode) is NOT a dispatch-catalog control: sending a
    # command straight at its key must refuse without any Alpaca call.
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "imaging_mode", "moon", controls_enabled=True, allow_power=True)
    assert result.status is DispatchStatus.REFUSED
    assert alpaca.calls == []


def test_alpaca_error_surfaces_as_error_status():
    # A dispatched command whose Alpaca call raises returns ERROR (not OK, not a
    # crash): the scope is never left in an unknown state silently.
    alpaca = _StubAlpaca(raise_exc=RuntimeError("scope busy"))
    result = dispatch(
        alpaca, "start_stack", {}, controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.ERROR
    assert "scope busy" in result.reason
    assert len(alpaca.calls) == 1  # it was attempted, then failed


# -- representative mappings ------------------------------------------------------

def test_run_plan_imports_then_starts_scheduler():
    # Run plan is a two-step action: import_schedule (with the named plan rooted
    # in seestar_alp's own schedule/ directory) THEN start_scheduler.
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "run_plan", "orion.json", controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    names = [name for name, _ in alpaca.calls]
    assert names == ["import_schedule", "start_scheduler"]
    import_params = alpaca.calls[0][1]
    assert import_params["filepath"] == "schedule/orion.json"


def test_run_plan_appends_json_suffix_to_bare_name():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "run_plan", "orion", controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    assert alpaca.calls[0][1]["filepath"] == "schedule/orion.json"


@pytest.mark.parametrize("evil", [
    "../../etc/passwd",         # relative traversal
    "/etc/passwd",              # absolute path
    "subdir/plan.json",         # any separator escapes the basename constraint
    "..\\..\\secrets.json",     # windows-style separator
    "~root/plan.json",          # home expansion
    ".hidden.json",             # dotfile
    "",                         # empty
    "   ",                      # whitespace only
])
def test_run_plan_refuses_path_traversal_and_non_basenames(evil):
    # seestar_alp's import_schedule does open(filepath) VERBATIM, so anything but
    # a bare plan name must refuse BEFORE any Alpaca call.
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "run_plan", evil, controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.REFUSED
    assert result.reason
    assert alpaca.calls == []  # CRITICAL: the traversal never reached the scope


def test_start_live_view_uses_the_stored_mode():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "start_live_view", "PRESS", controls_enabled=True,
        allow_power=False, stored={control.IMAGING_MODE_KEY: "moon"})
    assert result.status is DispatchStatus.OK
    name, params = alpaca.calls[0]
    assert name == "method_sync"
    assert params["method"] == "iscope_start_view"
    assert params["params"]["mode"] == "moon"


def test_start_live_view_defaults_to_star_when_mode_unset():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "start_live_view", "PRESS", controls_enabled=True,
        allow_power=False, stored={})
    assert result.status is DispatchStatus.OK
    assert alpaca.calls[0][1]["params"]["mode"] == "star"


# -- goto: real coordinates only ---------------------------------------------------

def _goto_stored(target="M31", ra="0.7123", dec="41.269"):
    return {
        control.GOTO_TARGET_KEY: target,
        control.GOTO_RA_KEY: ra,
        control.GOTO_DEC_KEY: dec,
    }


def test_goto_with_valid_decimal_coords_issues_goto_target():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "goto", "PRESS", controls_enabled=True, allow_power=False,
        stored=_goto_stored())
    assert result.status is DispatchStatus.OK
    name, params = alpaca.calls[0]
    assert name == "goto_target"
    assert params["target_name"] == "M31"
    assert params["is_j2000"] is True
    assert params["ra"] == pytest.approx(0.7123)
    assert params["dec"] == pytest.approx(41.269)


def test_goto_parses_sexagesimal_coordinates():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "goto", "PRESS", controls_enabled=True, allow_power=False,
        stored=_goto_stored(ra="0h42m44s", dec="+41d16m9s"))
    assert result.status is DispatchStatus.OK
    params = alpaca.calls[0][1]
    assert params["ra"] == pytest.approx(0 + 42 / 60 + 44 / 3600)
    assert params["dec"] == pytest.approx(41 + 16 / 60 + 9 / 3600)


def test_goto_parses_colon_separated_negative_dec():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "goto", "PRESS", controls_enabled=True, allow_power=False,
        stored=_goto_stored(ra="5:35:17", dec="-05:23:28"))
    assert result.status is DispatchStatus.OK
    params = alpaca.calls[0][1]
    assert params["ra"] == pytest.approx(5 + 35 / 60 + 17 / 3600)
    assert params["dec"] == pytest.approx(-(5 + 23 / 60 + 28 / 3600))


def test_goto_without_coordinates_is_refused_never_fabricated():
    # goto_target does NOT resolve names; without real coordinates the command
    # must refuse — a fabricated ra=0/dec=0 would slew to a real (wrong) place.
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "goto", "PRESS", controls_enabled=True, allow_power=False,
        stored={control.GOTO_TARGET_KEY: "M31"})  # name only, no RA/Dec
    assert result.status is DispatchStatus.REFUSED
    assert result.reason
    assert alpaca.calls == []  # CRITICAL: nothing reached the scope


def test_goto_with_unparseable_coordinates_is_refused():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "goto", "PRESS", controls_enabled=True, allow_power=False,
        stored=_goto_stored(ra="Andromeda", dec="up a bit"))
    assert result.status is DispatchStatus.REFUSED
    assert alpaca.calls == []


@pytest.mark.parametrize("ra,dec", [
    ("25.0", "10.0"),    # RA beyond 24h
    ("-1.0", "10.0"),    # negative RA
    ("10.0", "91.0"),    # Dec beyond +90
    ("10.0", "-90.5"),   # Dec beyond -90
    ("nan", "10.0"),     # NaN never passes the range check
])
def test_goto_with_out_of_range_coordinates_is_refused(ra, dec):
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "goto", "PRESS", controls_enabled=True, allow_power=False,
        stored=_goto_stored(ra=ra, dec=dec))
    assert result.status is DispatchStatus.REFUSED
    assert alpaca.calls == []


def test_goto_without_a_name_uses_a_default_label():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "goto", "PRESS", controls_enabled=True, allow_power=False,
        stored=_goto_stored(target=""))
    assert result.status is DispatchStatus.OK
    assert alpaca.calls[0][1]["target_name"]  # a non-empty session label


# -- park vs shutdown ---------------------------------------------------------------

def test_park_is_power_gated_and_stows_the_mount_only():
    # Park maps to the MOUNT-STOW rpc (scope_park — the same command seestar_alp's
    # own shut_down_thread parks with), NOT to the Pi power-off.
    park = _control("park")
    assert park.power_gated is True
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "park", {}, controls_enabled=True, allow_power=True)
    assert result.status is DispatchStatus.OK
    name, params = alpaca.calls[0]
    assert name == "method_sync"
    assert params["method"] == "scope_park"


def test_park_and_shutdown_dispatch_different_methods():
    # THE BLOCKER: park must never power off the Pi. The two power controls must
    # dispatch DIFFERENT methods, and park's calls must never contain pi_shutdown.
    park_alpaca = _StubAlpaca()
    dispatch(park_alpaca, "park", {}, controls_enabled=True, allow_power=True)
    shutdown_alpaca = _StubAlpaca()
    dispatch(shutdown_alpaca, "shutdown", {}, controls_enabled=True, allow_power=True)

    park_methods = [p.get("method") for _, p in park_alpaca.calls]
    shutdown_methods = [p.get("method") for _, p in shutdown_alpaca.calls]
    assert park_methods != shutdown_methods
    assert "pi_shutdown" not in park_methods  # park NEVER powers off the Pi
    assert park_methods == ["scope_park"]
    assert shutdown_methods == ["pi_shutdown"]


def test_dew_heater_switch_on_maps_to_heater_value():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "dew_heater_set", "ON", controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    name, params = alpaca.calls[0]
    assert name == "action_set_dew_heater"
    assert params["heater"] > 0


def test_dew_heater_switch_off_maps_to_zero_heater():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "dew_heater_set", "OFF", controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    _, params = alpaca.calls[0]
    assert params["heater"] == 0


def test_focus_number_maps_to_steps():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "focus", 30, controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    name, params = alpaca.calls[0]
    assert name == "adjust_focus"
    assert params["steps"] == 30


def test_stop_maps_to_stop_scheduler():
    alpaca = _StubAlpaca()
    result = dispatch(alpaca, "stop", {}, controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    assert alpaca.calls[0][0] == "stop_scheduler"


def test_startup_is_power_gated():
    assert _control("startup").power_gated is True


def test_every_control_key_is_unique():
    keys = [ctl.key for ctl in control.CONTROLS]
    assert len(keys) == len(set(keys))


#: Valid stored-input snapshot for the smoke dispatch of every trigger control.
_SAMPLE_STORED = {
    control.IMAGING_MODE_KEY: "moon",
    control.GOTO_TARGET_KEY: "M42",
    control.GOTO_RA_KEY: "5.591",
    control.GOTO_DEC_KEY: "-5.39",
}


def test_every_control_action_is_dispatchable_from_a_known_key():
    # Smoke: every catalog entry can be dispatched (gate open) and reaches Alpaca,
    # so no catalog entry references an action the dispatcher can't build params for.
    for ctl in control.CONTROLS:
        alpaca = _StubAlpaca()
        payload = _sample_payload(ctl)
        result = dispatch(
            alpaca, ctl.key, payload, controls_enabled=True, allow_power=True,
            stored=_SAMPLE_STORED)
        assert result.status is DispatchStatus.OK, f"{ctl.key} did not dispatch"
        assert alpaca.calls, f"{ctl.key} reached no Alpaca action"


def _sample_payload(ctl):
    """A representative valid payload for a control, by component."""
    if ctl.component == control.COMPONENT_NUMBER:
        return ctl.min_value
    if ctl.component == control.COMPONENT_SELECT:
        return ctl.options[0]
    if ctl.component == control.COMPONENT_SWITCH:
        return "ON"
    if ctl.component == control.COMPONENT_TEXT:
        return "M42"
    return {}  # button


def test_stored_input_keys_do_not_collide_with_dispatch_controls():
    dispatch_keys = {ctl.key for ctl in control.CONTROLS}
    stored_keys = {si.key for si in control.STORED_INPUTS}
    assert dispatch_keys.isdisjoint(stored_keys)


def test_exposure_is_milliseconds_passed_through():
    # action_set_exposure's ``exp`` is MILLISECONDS on the wire; the control's ms
    # value is passed through untouched (no unit conversion to get wrong).
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "exposure", "30000", controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    name, params = alpaca.calls[0]
    assert name == "action_set_exposure"
    assert params["exp"] == 30000


def test_exposure_range_spans_solar_to_deep_sky_ms():
    # 1 ms (solar) .. 60000 ms (60 s deep-sky): the range must make the scope's
    # real exposure domain reachable, in the SAME unit the wire uses.
    exposure = _control("exposure")
    assert exposure.min_value == control.EXPOSURE_MIN_MS == 1
    assert exposure.max_value == control.EXPOSURE_MAX_MS == 60_000


# -- discovery: command entities --------------------------------------------------

def _control_payload(key):
    ctl = None
    for entry in CONTROL_ENTITIES:
        if entry.key == key:
            ctl = entry
            break
    assert ctl is not None, key
    block = device_block(DEVICE_ID, DEVICE_NAME)
    return ctl, control_discovery_payload(
        ctl,
        device_block=block,
        base_topic=BASE_TOPIC,
        bridge_availability_topic=BRIDGE_AVAILABILITY_TOPIC,
    )


def test_button_discovery_has_command_topic_and_no_state():
    ctl, payload = _control_payload("start_stack")
    assert ctl.component == "button"
    assert payload["command_topic"] == f"{BASE_TOPIC}/cmd/start_stack"
    assert "state_topic" not in payload
    # A button is momentary: it carries a press payload, not on/off state.
    assert "payload_press" in payload


def test_switch_discovery_has_both_command_and_state_topics():
    ctl, payload = _control_payload("dew_heater_set")
    assert ctl.component == "switch"
    assert payload["command_topic"] == f"{BASE_TOPIC}/cmd/dew_heater_set"
    assert payload["state_topic"] == f"{BASE_TOPIC}/cmd/dew_heater_set/state"


def test_safety_switches_present_and_default_off():
    keys = {entry.key for entry in CONTROL_ENTITIES}
    assert "controls_enabled" in keys
    assert "allow_power" in keys
    for key in ("controls_enabled", "allow_power"):
        _, payload = _control_payload(key)
        assert payload["command_topic"] == f"{BASE_TOPIC}/cmd/{key}"
        assert payload["state_topic"] == f"{BASE_TOPIC}/cmd/{key}/state"


def test_number_discovery_carries_range_and_step():
    ctl, payload = _control_payload("exposure")
    assert ctl.component == "number"
    assert payload["min"] == ctl.min_value
    assert payload["max"] == ctl.max_value
    assert payload["step"] == ctl.step


def test_exposure_discovery_unit_and_range_match_the_dispatched_ms():
    # MAJOR: the HA number's label/range/unit must match what action_set_exposure
    # actually receives (milliseconds, passed through).
    ctl, payload = _control_payload("exposure")
    assert payload["unit_of_measurement"] == "ms"
    assert payload["min"] == control.EXPOSURE_MIN_MS
    assert payload["max"] == control.EXPOSURE_MAX_MS


def test_goto_entities_pair_value_texts_with_a_trigger_button():
    # The goto surface: three stored value texts (name + REAL coordinates) and a
    # momentary trigger button, so no fabricated coordinate can ever be sent.
    for key in ("goto_target", "goto_ra", "goto_dec"):
        ctl, payload = _control_payload(key)
        assert ctl.component == "text", key
        assert payload["state_topic"] == f"{BASE_TOPIC}/cmd/{key}/state"
    button, payload = _control_payload("goto")
    assert button.component == "button"
    assert "state_topic" not in payload


def test_imaging_mode_select_options_match_the_dispatch_modes():
    ctl, _payload = _control_payload("imaging_mode")
    assert ctl.component == "select"
    stored = control.stored_input_for("imaging_mode")
    assert stored is not None
    assert ctl.options == stored.options


def test_start_live_view_is_a_momentary_button():
    # MAJOR: selecting a mode must not start a session; the session starter is a
    # separate button (which reads the stored mode at press time).
    ctl, payload = _control_payload("start_live_view")
    assert ctl.component == "button"
    assert "payload_press" in payload


def test_select_discovery_carries_options():
    ctl, payload = _control_payload("imaging_mode")
    assert ctl.component == "select"
    assert payload["options"] == list(ctl.options)


def test_control_discovery_availability_lists_both_topics_with_mode_all():
    _, payload = _control_payload("start_stack")
    assert payload["availability_mode"] == "all"
    topics = [entry["topic"] for entry in payload["availability"]]
    assert topics == [BRIDGE_AVAILABILITY_TOPIC, f"{BASE_TOPIC}/availability"]


def test_control_discovery_namespaces_unique_id_by_device():
    _, payload = _control_payload("start_stack")
    assert payload["unique_id"] == f"{DEVICE_ID}_start_stack"
    assert payload["object_id"] == f"{DEVICE_ID}_start_stack"


def test_control_entities_do_not_collide_with_phase1_entities():
    from seestar_bridge.entities import ENTITIES

    phase1_keys = {entity.key for entity in ENTITIES}
    control_keys = {entry.key for entry in CONTROL_ENTITIES}
    assert phase1_keys.isdisjoint(control_keys)


@pytest.mark.parametrize("safety_key", ["controls_enabled", "allow_power"])
def test_safety_switches_are_not_in_the_dispatch_catalog(safety_key):
    # The safety switches gate dispatch; they are NOT themselves dispatchable
    # controls (dispatching them would recurse the gate onto the gate).
    dispatch_keys = {ctl.key for ctl in control.CONTROLS}
    assert safety_key not in dispatch_keys
