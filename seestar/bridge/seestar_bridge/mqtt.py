"""paho-mqtt client factory.

Isolated in its own module so the pure state extraction in ``scope.py`` stays
importable without paho. Ported from the validated Phase-1 ``build_client``: the
VERSION2 callback API, optional TLS to the broker (system CA — only the bundled
self-signed broker would need a custom CA), username/password auth, and a
Last-Will-and-Testament that marks availability ``offline`` if the bridge drops.

The orchestrator runs one shared client for all scopes. A single MQTT connection
carries exactly one will, so the factory takes the will topic explicitly; each
scope additionally republishes its own availability every cycle, so a scope's
``offline`` LWT is a backstop, not the only signal.
"""
from __future__ import annotations

import os
import ssl

import paho.mqtt.client as mqtt

from .settings import MqttSettings

_PAYLOAD_NOT_AVAILABLE = "offline"

#: paho reconnect backoff bounds (seconds): retry quickly, cap at a calm ceiling.
_RECONNECT_MIN_DELAY_SEC = 1
_RECONNECT_MAX_DELAY_SEC = 30

#: Stable-ish client id; the pid keeps two bridges on one host from colliding.
_CLIENT_ID_PREFIX = "seestar-bridge"


def build_client(mqtt_settings: MqttSettings, *, will_topic: str | None = None) -> mqtt.Client:
    """Build (but do not connect) a paho client from resolved MQTT settings.

    TLS uses the system CA store (``CERT_REQUIRED``); ``ssl=False`` connects in
    the clear (typical for the in-cluster Mosquitto add-on). When ``will_topic``
    is given, the client registers a retained ``offline`` LWT on it.
    """
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"{_CLIENT_ID_PREFIX}-{os.getpid()}",
    )
    if mqtt_settings.username:
        client.username_pw_set(mqtt_settings.username, mqtt_settings.password)
    if mqtt_settings.ssl:
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    if will_topic:
        client.will_set(will_topic, _PAYLOAD_NOT_AVAILABLE, retain=True)
    client.reconnect_delay_set(
        min_delay=_RECONNECT_MIN_DELAY_SEC, max_delay=_RECONNECT_MAX_DELAY_SEC)
    return client
