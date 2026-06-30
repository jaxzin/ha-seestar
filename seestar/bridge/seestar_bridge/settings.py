"""Add-on options + environment → resolved bridge settings.

Two config sources feed the bridge:

- **Add-on options** (``options`` dict): the user-facing config from the HA
  add-on ``config.yaml`` schema — which scopes to bundle, whether to reuse an
  external seestar_alp (``alpaca_host``), poll cadences, and optional explicit
  MQTT overrides (``mqtt_*``).
- **Environment** (``env`` dict): the bashio ``init-config`` oneshot resolves
  the Supervisor MQTT service and exports it as ``MQTT_HOST/PORT/USERNAME/
  PASSWORD/SSL``. We read those only when the options don't pin MQTT explicitly.

``load_settings`` collapses both into a single :class:`Settings`, applying the
spec defaults and failing fast (``ValueError``) when MQTT can't be resolved or
when the operator pinned neither ``scopes`` (bundled) nor ``alpaca_host``
(external).
"""
from dataclasses import dataclass

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

# Bundled seestar_alp binds Alpaca + the web UI to these ports (see config.toml /
# the init-config oneshot). In external mode the Alpaca port comes from
# ``alpaca_host`` and only the web-UI port keeps this default.
BUNDLED_ALPACA_HOST = "localhost:5555"
DEFAULT_ALPACA_WEBUI_PORT = 5432

# Where the bundled seestar_alp config.toml lives inside the image; the
# init-config oneshot writes it from the ``scopes`` option, and discovery reads
# it back for scope-address resolution. Only meaningful in bundled mode.
BUNDLED_CONFIG_TOML_PATH = "/app/seestar_alp/device/config.toml"

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
    alpaca_base: str
    webui_base: str | None
    config_toml_path: str | None
    discovery_prefix: str
    event_poll_sec: int
    state_poll_sec: int
    preview_max_px: int
    log_level: str
    mqtt: MqttSettings


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

    Bundled mode (``alpaca_host`` blank, ``scopes`` set): Alpaca is the bundled
    driver on ``localhost:5555`` and we manage its config.toml. External mode
    (``alpaca_host`` set): point Alpaca at that host and leave config.toml alone.
    The web-UI base (preview only) is derived from the Alpaca host plus
    ``alpaca_webui_port``.
    """
    alpaca_host = options.get("alpaca_host") or ""
    has_scopes = bool(options.get("scopes"))
    if not alpaca_host and not has_scopes:
        raise ValueError(
            "No telescope configured: add at least one entry under the scopes "
            "option (bundled driver) or set alpaca_host to an external "
            "seestar_alp (host:port)."
        )

    bundled = not alpaca_host
    host_port = BUNDLED_ALPACA_HOST if bundled else alpaca_host
    alpaca_base = f"http://{host_port}"
    config_toml_path = BUNDLED_CONFIG_TOML_PATH if bundled else None

    webui_port = int(options.get("alpaca_webui_port", DEFAULT_ALPACA_WEBUI_PORT))
    webui_base = f"http://{_host_of(host_port)}:{webui_port}"

    return Settings(
        alpaca_base=alpaca_base,
        webui_base=webui_base,
        config_toml_path=config_toml_path,
        discovery_prefix=options.get("discovery_prefix", DEFAULT_DISCOVERY_PREFIX),
        event_poll_sec=int(options.get("event_poll_sec", DEFAULT_EVENT_POLL_SEC)),
        state_poll_sec=int(options.get("state_poll_sec", DEFAULT_STATE_POLL_SEC)),
        preview_max_px=int(options.get("preview_max_px", DEFAULT_PREVIEW_MAX_PX)),
        log_level=_resolve_log_level(options.get("log_level")),
        mqtt=_resolve_mqtt(options, env),
    )
