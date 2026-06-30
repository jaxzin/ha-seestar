import pytest

from seestar_bridge.settings import MqttSettings, Settings, load_settings


def _mqtt_env():
    """Supervisor-style MQTT env, as the bashio init exports it."""
    return {
        "MQTT_HOST": "core-mosquitto",
        "MQTT_PORT": "1883",
        "MQTT_USERNAME": "svc",
        "MQTT_PASSWORD": "svcpw",
        "MQTT_SSL": "false",
    }


def test_bundled_defaults():
    # No alpaca_host => bundled driver: alpaca on localhost:5555, config.toml path set.
    options = {"scopes": [{"name": "Backyard"}]}
    s = load_settings(options, _mqtt_env())

    assert isinstance(s, Settings)
    assert s.alpaca_base == "http://localhost:5555"
    assert s.config_toml_path is not None
    # webui derived from the same host as alpaca + the default webui port.
    assert s.webui_base == "http://localhost:5432"
    # Spec defaults.
    assert s.discovery_prefix == "homeassistant"
    assert s.event_poll_sec == 10
    assert s.state_poll_sec == 30
    assert s.preview_max_px == 1280


def test_external_mode_no_config_toml_and_webui_derived():
    # alpaca_host set => don't manage seestar_alp config; config_toml_path None,
    # webui derived from the external host.
    options = {"alpaca_host": "scopebox:5555"}
    s = load_settings(options, _mqtt_env())

    assert s.alpaca_base == "http://scopebox:5555"
    assert s.config_toml_path is None
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
    options = {"scopes": [{"name": "Backyard"}]}
    s = load_settings(options, _mqtt_env())

    assert s.mqtt == MqttSettings(
        host="core-mosquitto",
        port=1883,
        username="svc",
        password="svcpw",
        ssl=False,
    )


def test_valueerror_when_mqtt_unresolved():
    options = {"scopes": [{"name": "Backyard"}]}
    with pytest.raises(ValueError, match="MQTT"):
        load_settings(options, {})


def test_valueerror_when_neither_scopes_nor_alpaca_host():
    options = {}
    with pytest.raises(ValueError, match="scopes"):
        load_settings(options, _mqtt_env())


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
    options = {"scopes": [{"name": "Backyard"}]}
    env = {**_mqtt_env(), "MQTT_PORT": port_value}
    s = load_settings(options, env)

    assert s.mqtt.host == "core-mosquitto"
    assert s.mqtt.port == 1883


def test_log_level_defaults_to_info():
    options = {"scopes": [{"name": "Backyard"}]}
    s = load_settings(options, _mqtt_env())

    assert s.log_level == "info"


def test_log_level_flows_through_normalized():
    # An operator-supplied level flows into Settings, normalized to lowercase.
    options = {"scopes": [{"name": "Backyard"}], "log_level": "DEBUG"}
    s = load_settings(options, _mqtt_env())

    assert s.log_level == "debug"


def test_log_level_invalid_rejected():
    options = {"scopes": [{"name": "Backyard"}], "log_level": "verbose"}
    with pytest.raises(ValueError, match="log_level"):
        load_settings(options, _mqtt_env())
