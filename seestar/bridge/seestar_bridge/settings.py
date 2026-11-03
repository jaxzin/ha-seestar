"""Add-on options + environment → resolved bridge settings.

Two config sources feed the bridge:

- **Add-on options** (``options`` dict): the user-facing config from the HA
  add-on ``config.yaml`` schema — an optional explicit seestar_alp endpoint
  (``alpaca_host``), poll cadences, and optional explicit MQTT overrides
  (``mqtt_*``).
- **Environment** (``env`` dict): the bashio ``init-config`` oneshot resolves
  the Supervisor MQTT service and exports it as ``MQTT_HOST/PORT/USERNAME/
  PASSWORD/SSL``. We read those only when the options don't pin MQTT explicitly.

``load_settings`` collapses both into a single :class:`Settings`, applying the
spec defaults and failing fast (``ValueError``) when MQTT can't be resolved.
With ``alpaca_host`` blank the Alpaca endpoint is left unresolved
(``alpaca_base is None``) for :mod:`seestar_bridge.alpaca_discovery` to find;
:func:`with_alpaca_endpoint` then fills it in.
"""
from dataclasses import dataclass, replace

# --- spec defaults -----------------------------------------------------------
DEFAULT_DISCOVERY_PREFIX = "homeassistant"
DEFAULT_EVENT_POLL_SEC = 10
DEFAULT_STATE_POLL_SEC = 30
DEFAULT_PREVIEW_MAX_PX = 1280
DEFAULT_LOG_LEVEL = "info"
DEFAULT_MQTT_PORT = 1883

# Levels the config.yaml schema offers (bashio list). Anything outside this set
# is rejected so a typo fails fast instead of silently degrading to a default.
VALID_LOG_LEVELS = ("trace", "debug", "info", "notice", "warning", "error", "fatal")

# The HA add-on log_level convention (bashio) offers three levels that are NOT
# stdlib ``logging`` level names: ``trace`` and ``notice`` don't exist there, and
# ``fatal`` is only an alias HA uses by convention. Map each onto the nearest
# stdlib level so ``getattr(logging, NAME)`` resolves for every schema value
# instead of silently falling back to INFO. Levels already valid in ``logging``
# (debug/info/warning/error) map to themselves.
_HA_TO_PYTHON_LEVEL = {
    "trace": "debug",
    "notice": "info",
    "fatal": "critical",
}

# seestar_alp's stock ports: the web UI (where ``/config.json`` lives, for the
# preview address) and the imaging server (the live ``/vid`` MJPEG stream) both
# bind on the SAME host as the Alpaca endpoint. The options only need changing
# for a seestar_alp rebound elsewhere.
DEFAULT_ALPACA_WEBUI_PORT = 5432
DEFAULT_IMAGING_PORT = 7556

_TRUE_TOKENS = ("1", "true", "yes", "on")


@dataclass(frozen=True)
class MqttSettings:
    host: str
    port: int
    username: str
    password: str
    ssl: bool


@dataclass(frozen=True)
class Settings:
    #: ``http://host:port`` of seestar_alp's Alpaca API; ``None`` until resolved
    #: (``alpaca_host`` blank → discovered at startup, see with_alpaca_endpoint).
    alpaca_base: str | None
    #: ``http://host:webui_port`` of seestar_alp's web UI (preview address
    #: lookup); ``None`` until the Alpaca endpoint is known.
    webui_base: str | None
    discovery_prefix: str
    event_poll_sec: int
    state_poll_sec: int
    preview_max_px: int
    log_level: str
    mqtt: MqttSettings
    #: Port of seestar_alp's imaging server on the Alpaca host; the live camera
    #: grabs frames from ``http://<alpaca host>:<imaging_port>/<device_num>/vid``.
    imaging_port: int = DEFAULT_IMAGING_PORT
    alpaca_webui_port: int = DEFAULT_ALPACA_WEBUI_PORT


def with_alpaca_endpoint(settings: Settings, host_port: str) -> Settings:
    """Return ``settings`` bound to the seestar_alp at ``host_port``.

    Derives the Alpaca base and the web-UI base (same host, the
    ``alpaca_webui_port``) from one ``host:port`` string.
    """
    return replace(
        settings,
        alpaca_base=f"http://{host_port}",
        webui_base=f"http://{_host_of(host_port)}:{settings.alpaca_webui_port}",
    )


def _as_bool(value, default=False):
    """Coerce a YAML/env value to bool, accepting native bools and string tokens."""
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in _TRUE_TOKENS


def _host_of(host_port):
    """Return the bare host from a ``host`` or ``host:port`` string."""
    return host_port.rsplit(":", 1)[0] if ":" in host_port else host_port


def _resolve_port(value):
    """Coerce a port value to int, treating 0/blank/None as unset.

    Supervisor always passes ``mqtt_port`` present (config.yaml default ``0``) and
    the bashio env exports ``MQTT_PORT`` as a string, so an operator who sets
    ``mqtt_host`` but leaves the port at its ``0`` default would otherwise resolve
    to an invalid port ``0``. Treat that — and an empty string — as "use the
    standard MQTT port".
    """
    if value in (None, "", 0, "0"):
        return DEFAULT_MQTT_PORT
    return int(value)


def _resolve_log_level(value):
    """Normalize a log-level option to a known lowercase level, defaulting to info.

    Rejects an unrecognized level so a typo fails fast (``ValueError``) rather than
    silently degrading. Returned lowercase; callers uppercase it for the stdlib
    ``logging`` module and the upstream driver's ``[logging] log_level`` key.
    """
    if not value:
        return DEFAULT_LOG_LEVEL
    level = str(value).strip().lower()
    if level not in VALID_LOG_LEVELS:
        raise ValueError(
            f"Invalid log_level '{value}': choose one of {', '.join(VALID_LOG_LEVELS)}."
        )
    return level


def python_log_level(level):
    """Map a resolved (lowercase) log level to a stdlib ``logging`` level name.

    ``trace``/``notice``/``fatal`` are HA-convention levels with no stdlib
    counterpart of the same name, so ``getattr(logging, "TRACE")`` would miss and
    callers would silently fall back to INFO. Translate those to the nearest real
    level (``debug``/``info``/``critical``); every other value is already a valid
    ``logging`` name and passes through unchanged. Returned UPPERCASE, ready for
    ``getattr(logging, ...)`` and the upstream driver's ``[logging] log_level``.
    """
    return _HA_TO_PYTHON_LEVEL.get(level, level).upper()


def _resolve_mqtt(options, env):
    """MQTT from ``mqtt_*`` options when host is set, else from the Supervisor env.

    Fails fast with an actionable message when neither source supplies a host:
    without a broker the bridge has nothing to publish to.
    """
    opt_host = options.get("mqtt_host")
    if opt_host:
        return MqttSettings(
            host=opt_host,
            port=_resolve_port(options.get("mqtt_port")),
            username=options.get("mqtt_username", "") or "",
            password=options.get("mqtt_password", "") or "",
            ssl=_as_bool(options.get("mqtt_ssl")),
        )

    env_host = env.get("MQTT_HOST")
    if env_host:
        return MqttSettings(
            host=env_host,
            port=_resolve_port(env.get("MQTT_PORT")),
            username=env.get("MQTT_USERNAME", "") or "",
            password=env.get("MQTT_PASSWORD", "") or "",
            ssl=_as_bool(env.get("MQTT_SSL")),
        )

    raise ValueError(
        "MQTT broker could not be resolved: install the official Mosquitto add-on "
        "(so the Supervisor MQTT service is available) or set the mqtt_host/mqtt_port/"
        "mqtt_username/mqtt_password options."
    )


def load_settings(options, env):
    """Resolve add-on ``options`` + ``env`` into a :class:`Settings`.

    ``alpaca_host`` set: bind to that seestar_alp. Blank: leave the endpoint
    unresolved for startup discovery.
    """
    settings = Settings(
        alpaca_base=None,
        webui_base=None,
        discovery_prefix=options.get("discovery_prefix", DEFAULT_DISCOVERY_PREFIX),
        event_poll_sec=int(options.get("event_poll_sec", DEFAULT_EVENT_POLL_SEC)),
        state_poll_sec=int(options.get("state_poll_sec", DEFAULT_STATE_POLL_SEC)),
        preview_max_px=int(options.get("preview_max_px", DEFAULT_PREVIEW_MAX_PX)),
        log_level=_resolve_log_level(options.get("log_level")),
        mqtt=_resolve_mqtt(options, env),
        imaging_port=int(options.get("imaging_port", DEFAULT_IMAGING_PORT)),
        alpaca_webui_port=int(options.get("alpaca_webui_port", DEFAULT_ALPACA_WEBUI_PORT)),
    )
    alpaca_host = (options.get("alpaca_host") or "").strip()
    return with_alpaca_endpoint(settings, alpaca_host) if alpaca_host else settings
