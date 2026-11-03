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


def _connect(client, reason_code=0):
    client.on_connect(client, None, None, reason_code, None)


def test_birth_published_retained_on_every_connect_and_reconnect(monkeypatch):
    client = build_client(
        _MQTT, will_topic="seestar/bridge/availability",
        will_payload="offline", birth_payload="online")
    published = []
    monkeypatch.setattr(
        client, "publish", lambda t, p, retain=False, qos=0: published.append((t, p, retain)))
    _connect(client)
    _connect(client)  # reconnect after the broker flipped the topic to the will
    assert published == [("seestar/bridge/availability", "online", True)] * 2


def test_birth_not_published_when_connack_refused(monkeypatch):
    client = build_client(
        _MQTT, will_topic="seestar/bridge/availability", birth_payload="online")
    published = []
    monkeypatch.setattr(client, "publish", lambda *a, **k: published.append(a))
    _connect(client, reason_code=5)
    assert published == []


def test_set_router_keeps_birth_on_reconnect(monkeypatch):
    from seestar_bridge.mqtt import set_router

    client = build_client(
        _MQTT, will_topic="seestar/bridge/availability", birth_payload="online")
    published, subscribed = [], []
    monkeypatch.setattr(
        client, "publish", lambda t, p, retain=False, qos=0: published.append((t, p, retain)))
    monkeypatch.setattr(client, "subscribe", lambda t, qos=0: subscribed.append(t))
    set_router(client, lambda t, p: None, ["seestar/x/cmd/#"])
    _connect(client)
    assert published == [("seestar/bridge/availability", "online", True)]
    assert subscribed == ["seestar/x/cmd/#"]
