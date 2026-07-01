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

Phase 2 adds the command (subscribe) path. The same shared client that publishes
telemetry now also SUBSCRIBEs to every scope's command topics: :func:`set_router`
installs a single ``on_connect``/``on_message`` pair so that (a) on every connect
AND reconnect the client (re)subscribes to the caller's command-topic filters,
and (b) each inbound ``(topic, payload)`` is handed to the caller's handler. The
handler is fully guarded here so a bad command can never crash paho's network
loop or take the shared client down for the other scopes.
"""
from __future__ import annotations

import logging
import os
import ssl
from collections.abc import Callable, Sequence

import paho.mqtt.client as mqtt

from .settings import MqttSettings

_log = logging.getLogger(__name__)

_PAYLOAD_NOT_AVAILABLE = "offline"

#: paho reconnect backoff bounds (seconds): retry quickly, cap at a calm ceiling.
_RECONNECT_MIN_DELAY_SEC = 1
_RECONNECT_MAX_DELAY_SEC = 30

#: Stable-ish client id; the pid keeps two bridges on one host from colliding.
_CLIENT_ID_PREFIX = "seestar-bridge"

#: QoS 1 for command subscriptions: a control message that MOVES A TELESCOPE must
#: not be silently dropped by an at-most-once delivery, so we ask the broker for
#: at-least-once. Handlers are idempotent enough (a repeated button press re-runs
#: the same /action) that the duplicate risk is acceptable versus a lost command.
COMMAND_QOS = 1

#: A router handler maps one inbound ``(topic, payload)`` to a side effect. It is
#: invoked from paho's network thread; :func:`set_router` guards every call so a
#: raised exception is logged, never propagated into the loop.
MessageHandler = Callable[[str, str], None]


def build_client(
    mqtt_settings: MqttSettings,
    *,
    will_topic: str | None = None,
    will_payload: str = _PAYLOAD_NOT_AVAILABLE,
) -> mqtt.Client:
    """Build (but do not connect) a paho client from resolved MQTT settings.

    TLS uses the system CA store (``CERT_REQUIRED``); ``ssl=False`` connects in
    the clear (typical for the in-cluster Mosquitto add-on). When ``will_topic``
    is given, the client registers a retained Last-Will on it (``will_payload``,
    defaulting to ``offline``), so the broker marks the bridge offline if the
    process dies without a clean disconnect.
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
        client.will_set(will_topic, will_payload, retain=True)
    client.reconnect_delay_set(
        min_delay=_RECONNECT_MIN_DELAY_SEC, max_delay=_RECONNECT_MAX_DELAY_SEC)
    return client


def set_router(
    client: mqtt.Client,
    handler: MessageHandler,
    command_filters: Sequence[str],
) -> None:
    """Wire the shared client to SUBSCRIBE to command topics and route messages.

    Installs a single ``on_connect``/``on_message`` pair on ``client``:

    - ``on_connect`` (re)subscribes to every filter in ``command_filters`` at
      :data:`COMMAND_QOS`. Doing this in the callback — not once after
      ``connect()`` — means the subscriptions are re-established automatically on
      every paho reconnect, so a dropped-and-restored broker link never leaves a
      scope deaf to commands.
    - ``on_message`` decodes the payload and hands ``(topic, payload)`` to
      ``handler``. The call is wrapped so ANY exception the handler raises is
      logged and swallowed: a single malformed command must never crash paho's
      network loop or the shared client that every scope depends on.

    ``command_filters`` are MQTT topic filters (e.g. ``seestar/<device>/cmd/#``),
    one per scope; the caller owns the mapping from a concrete topic back to a
    scope worker.
    """
    filters = tuple(command_filters)

    def _on_connect(_client, _userdata, _flags, reason_code, _properties=None):
        # A non-zero reason_code means the CONNACK was refused; there is nothing
        # to subscribe to, so log and return rather than silently no-op.
        if reason_code != 0:
            _log.warning("MQTT connect refused (reason=%s); not subscribing", reason_code)
            return
        for topic_filter in filters:
            client.subscribe(topic_filter, qos=COMMAND_QOS)
            _log.info("subscribed to command topics: %s", topic_filter)

    def _on_message(_client, _userdata, message):
        topic = message.topic
        try:
            payload = message.payload.decode("utf-8")
        except UnicodeDecodeError:
            _log.warning("dropping non-UTF-8 command on %s", topic)
            return
        try:
            handler(topic, payload)
        except Exception:  # noqa: BLE001 — the network loop must survive any handler bug
            # A command that MOVES A TELESCOPE failing is bad, but taking the whole
            # shared client down with it is worse: log with the topic and continue.
            _log.exception("command handler raised on %s; shared client kept alive", topic)

    client.on_connect = _on_connect
    client.on_message = _on_message
