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


def test_select_rejects_value_outside_options():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "imaging_mode", "not_a_mode", controls_enabled=True, allow_power=False)
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
    # Run plan is a two-step action: import_schedule (with the selected plan's
    # filepath) THEN start_scheduler.
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "run_plan", "/plans/orion.json", controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    names = [name for name, _ in alpaca.calls]
    assert names == ["import_schedule", "start_scheduler"]
    import_params = alpaca.calls[0][1]
    assert import_params["filepath"] == "/plans/orion.json"


def test_start_live_view_passes_the_selected_mode():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "start_live_view", "moon", controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    name, params = alpaca.calls[0]
    assert name == "method_sync"
    assert params["method"] == "iscope_start_view"
    assert params["params"]["mode"] == "moon"


def test_goto_passes_the_target():
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "goto", "M31", controls_enabled=True, allow_power=False)
    assert result.status is DispatchStatus.OK
    name, params = alpaca.calls[0]
    assert name == "goto_target"
    assert params["target_name"] == "M31"


def test_park_is_power_gated_and_maps_to_shutdown_method():
    # Park is a power action; with both switches on it dispatches the documented
    # pi-shutdown method_sync call.
    park = _control("park")
    assert park.power_gated is True
    alpaca = _StubAlpaca()
    result = dispatch(
        alpaca, "park", {}, controls_enabled=True, allow_power=True)
    assert result.status is DispatchStatus.OK
    name, params = alpaca.calls[0]
    assert name == "method_sync"
    assert params["method"] == "pi_shutdown"


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


def test_every_control_action_is_dispatchable_from_a_known_key():
    # Smoke: every catalog entry can be dispatched (gate open) and reaches Alpaca,
    # so no catalog entry references an action the dispatcher can't build params for.
    for ctl in control.CONTROLS:
        alpaca = _StubAlpaca()
        payload = _sample_payload(ctl)
        result = dispatch(
            alpaca, ctl.key, payload, controls_enabled=True, allow_power=True)
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
