"""Tests for the paho client factory — focused on the bridge Last-Will.

``build_client`` is a thin factory, but the LWT it registers is load-bearing for
availability liveness: if the bridge process dies, the broker publishes the
will so every entity (which ANDs the bridge topic into its availability list)
goes unavailable. These tests pin that the will is registered with the topic,
payload, and retain flag the orchestrator passes.
"""
from seestar_bridge.mqtt import build_client
from seestar_bridge.settings import MqttSettings

_MQTT = MqttSettings(host="broker", port=1883, username="", password="", ssl=False)


def test_build_client_registers_lwt_with_topic_and_payload():
    client = build_client(_MQTT, will_topic="seestar/bridge/availability", will_payload="offline")
    assert client._will is True
    assert client._will_topic == b"seestar/bridge/availability"
    assert client._will_payload == b"offline"
    assert client._will_retain is True


def test_build_client_defaults_will_payload_to_offline():
    client = build_client(_MQTT, will_topic="seestar/bridge/availability")
    assert client._will_payload == b"offline"


def test_build_client_without_will_topic_registers_no_will():
    client = build_client(_MQTT)
    assert client._will is False
