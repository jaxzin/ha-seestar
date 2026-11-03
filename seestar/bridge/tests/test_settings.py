import pytest

from seestar_bridge.settings import (
    MqttSettings,
    Settings,
    load_settings,
    python_log_level,
    with_alpaca_endpoint,
)


def _mqtt_env():
    """Supervisor-style MQTT env, as the bashio init exports it."""
    return {
        "MQTT_HOST": "core-mosquitto",
        "MQTT_PORT": "1883",
        "MQTT_USERNAME": "svc",
        "MQTT_PASSWORD": "svcpw",
        "MQTT_SSL": "false",
    }


def test_blank_alpaca_host_leaves_endpoint_for_discovery():
    # No alpaca_host => nothing to bind yet: main() discovers the seestar_alp.
    s = load_settings({}, _mqtt_env())

    assert isinstance(s, Settings)
    assert s.alpaca_base is None
    assert s.webui_base is None
    # Spec defaults.
    assert s.discovery_prefix == "homeassistant"
    assert s.event_poll_sec == 10
    assert s.state_poll_sec == 30
    assert s.preview_max_px == 1280


def test_discovered_endpoint_derives_alpaca_and_webui_bases():
    # A discovered endpoint gets the same derivation as an explicit one,
    # including a non-default web UI port.
    s = load_settings({"alpaca_webui_port": 8080}, _mqtt_env())
    bound = with_alpaca_endpoint(s, "ff29c31e-seestar-alp:5555")

    assert bound.alpaca_base == "http://ff29c31e-seestar-alp:5555"
    assert bound.webui_base == "http://ff29c31e-seestar-alp:8080"


def test_explicit_alpaca_host_binds_and_derives_webui():
    options = {"alpaca_host": "scopebox:5555"}
    s = load_settings(options, _mqtt_env())

    assert s.alpaca_base == "http://scopebox:5555"
    assert s.webui_base == "http://scopebox:5432"


def test_external_mode_custom_webui_port():
    options = {"alpaca_host": "scopebox:5555", "alpaca_webui_port": 8080}
    s = load_settings(options, _mqtt_env())

    assert s.webui_base == "http://scopebox:8080"


def test_mqtt_override_beats_env():
    # mqtt_* options win over the Supervisor env.
    options = {
        "scopes": [{"name": "Backyard"}],
        "mqtt_host": "broker.example.com",
        "mqtt_port": 8883,
        "mqtt_username": "opt_user",
        "mqtt_password": "opt_pw",
        "mqtt_ssl": True,
    }
    s = load_settings(options, _mqtt_env())

    assert s.mqtt == MqttSettings(
        host="broker.example.com",
        port=8883,
        username="opt_user",
        password="opt_pw",
        ssl=True,
    )


def test_mqtt_from_env_when_options_blank():
    options = {}
    s = load_settings(options, _mqtt_env())

    assert s.mqtt == MqttSettings(
        host="core-mosquitto",
        port=1883,
        username="svc",
        password="svcpw",
        ssl=False,
    )


def test_valueerror_when_mqtt_unresolved():
    options = {}
    with pytest.raises(ValueError, match="MQTT"):
        load_settings(options, {})


def test_mqtt_port_zero_option_falls_back_to_default():
    # Supervisor always passes mqtt_port present (config.yaml default 0); when the
    # operator sets mqtt_host but leaves the port at 0, resolve the standard 1883
    # rather than the invalid port 0.
    options = {
        "scopes": [{"name": "Backyard"}],
        "mqtt_host": "broker.example.com",
        "mqtt_port": 0,
    }
    s = load_settings(options, _mqtt_env())

    assert s.mqtt.host == "broker.example.com"
    assert s.mqtt.port == 1883


@pytest.mark.parametrize("port_value", ["", "0"])
def test_mqtt_port_blank_or_zero_env_falls_back_to_default(port_value):
    # The bashio env exports MQTT_PORT as a string; '' or '0' must resolve to 1883.
    options = {}
    env = {**_mqtt_env(), "MQTT_PORT": port_value}
    s = load_settings(options, env)

    assert s.mqtt.host == "core-mosquitto"
    assert s.mqtt.port == 1883


def test_log_level_defaults_to_info():
    options = {}
    s = load_settings(options, _mqtt_env())

    assert s.log_level == "info"


def test_log_level_flows_through_normalized():
    # An operator-supplied level flows into Settings, normalized to lowercase.
    options = {"log_level": "DEBUG"}
    s = load_settings(options, _mqtt_env())

    assert s.log_level == "debug"


def test_log_level_invalid_rejected():
    options = {"log_level": "verbose"}
    with pytest.raises(ValueError, match="log_level"):
        load_settings(options, _mqtt_env())


@pytest.mark.parametrize("ha_level", ["trace", "debug", "info", "notice", "warning", "error", "fatal"])
def test_ha_level_options_are_all_accepted(ha_level):
    # Every value in the config.yaml log_level enum must load (lowercased into
    # Settings) rather than being rejected as an invalid level.
    options = {"log_level": ha_level}
    s = load_settings(options, _mqtt_env())

    assert s.log_level == ha_level


def test_python_log_level_maps_ha_only_levels():
    # The HA-convention levels with no stdlib name resolve to the nearest real one
    # (uppercased for getattr(logging, ...) / the driver's [logging] log_level).
    assert python_log_level("trace") == "DEBUG"
    assert python_log_level("notice") == "INFO"
    assert python_log_level("fatal") == "CRITICAL"


@pytest.mark.parametrize("level", ["debug", "info", "warning", "error"])
def test_python_log_level_passes_through_stdlib_levels(level):
    # Levels that are already valid logging names map to themselves (uppercased).
    assert python_log_level(level) == level.upper()


def test_python_log_level_resolves_to_a_real_logging_level():
    # End-to-end guard: every config.yaml enum value, once mapped, is a real
    # logging level (so getattr(logging, NAME) won't silently fall back to INFO).
    import logging

    for ha_level in ("trace", "debug", "info", "notice", "warning", "error", "fatal"):
        name = python_log_level(ha_level)
        assert isinstance(getattr(logging, name), int)
