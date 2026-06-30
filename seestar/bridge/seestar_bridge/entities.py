"""Entity catalog + MQTT discovery payloads — the HA contract for one Seestar.

Ported from the validated Phase-1 bridge (``seestar_events/bridge.py``). The
catalog (telephoto/wide cameras, stacking, plate-solve, plan, detected objects,
filter, pointing incl. computed Alt/Az + slew/park, health) and the discovery
``value_template`` logic are unchanged; what differs is that everything that was
a module-global in Phase 1 (the device id/name, base topic) is now a parameter,
so a single process can publish a distinct HA device per scope.

HA derives ``entity_id`` from the entity ``name`` and ignores ``object_id``; we
still set ``object_id``/``unique_id`` (namespaced by device id) for stable
re-discovery, but dashboards must read the real ids back from the registry.
"""
from __future__ import annotations

import re
from typing import Any, NamedTuple

# ---- MQTT discovery vocabulary (named, not inlined, so the contract is auditable)
_COMPONENT_BINARY_SENSOR = "binary_sensor"
_COMPONENT_SENSOR = "sensor"

_PAYLOAD_ON = "ON"
_PAYLOAD_OFF = "OFF"
_PAYLOAD_AVAILABLE = "online"
_PAYLOAD_NOT_AVAILABLE = "offline"

#: An entity is available only when EVERY topic in its availability list reports
#: ``online`` — i.e. both the bridge process is alive (its LWT topic) AND the
#: scope is reachable (the per-scope topic). ``"any"`` would mark it available
#: when only one is up, which would lie about a dead scope behind a live bridge.
_AVAILABILITY_MODE_ALL = "all"

# Sub-topics appended to a scope's base topic. Kept as constants so the producer
# (this module) and the consumer (the scope worker that publishes to these
# topics) agree on the wire format.
_STATE_SUBTOPIC = "state"
_AVAILABILITY_SUBTOPIC = "availability"


def availability_list(*topics: str) -> list[dict[str, str]]:
    """Build an MQTT-discovery ``availability`` list over one or more topics.

    Each entry uses the shared ``online``/``offline`` payloads. Combined with
    ``availability_mode: "all"`` (see :data:`_AVAILABILITY_MODE_ALL`), an entity
    is available only when every listed topic reports ``online`` — used to AND
    the bridge-level liveness (LWT) with the per-scope reachability signal.
    """
    return [
        {
            "topic": topic,
            "payload_available": _PAYLOAD_AVAILABLE,
            "payload_not_available": _PAYLOAD_NOT_AVAILABLE,
        }
        for topic in topics
    ]

# Static identity for the Seestar S30 Pro hardware. Per-scope identity (id/name)
# is injected via ``device_block`` so multiple scopes don't collide.
_MANUFACTURER = "ZWO"
_MODEL = "Seestar S30 Pro"

# value_template forms: binary sensors map truthiness to ON/OFF; plain sensors
# pass the value through, emitting "" for a missing (None) field so HA shows the
# entity as empty rather than the literal string "None".
_BINARY_TEMPLATE = "{{ '%s' if value_json.%%s else '%s' }}" % (_PAYLOAD_ON, _PAYLOAD_OFF)
_SENSOR_TEMPLATE = "{{ value_json.%s if value_json.%s is not none else '' }}"


class Entity(NamedTuple):
    """One HA entity in the catalog.

    Fields mirror the Phase-1 7-tuple ``(component, key, name, unit,
    device_class, state_class, icon)`` so the extraction code (Task 7) keys off
    ``key`` exactly as before, but named access (``entity.key``) keeps call
    sites readable.
    """

    component: str
    key: str
    name: str
    unit: str | None
    device_class: str | None
    state_class: str | None
    icon: str | None


# (component, key, name, unit, device_class, state_class, icon) — ported verbatim
# from the validated Phase-1 catalog.
ENTITIES: list[Entity] = [
    # Telephoto camera (cam 0 / View) -- the main imaging camera
    Entity("sensor", "telephoto_target", "Telephoto target", None, None, None, "mdi:image-filter-center-focus"),
    Entity("sensor", "telephoto_state", "Telephoto state", None, None, None, "mdi:camera"),
    Entity("sensor", "telephoto_mode", "Telephoto mode", None, None, None, "mdi:camera-iris"),
    Entity("sensor", "telephoto_gain", "Telephoto gain", None, None, "measurement", "mdi:brightness-6"),
    Entity("binary_sensor", "telephoto_lp", "Telephoto LP filter", None, None, None, "mdi:image-filter-vintage"),
    # Wide-field camera (cam 1 / SecondView)
    Entity("sensor", "wide_target", "Wide-field target", None, None, None, "mdi:image-filter-center-focus-weak"),
    Entity("sensor", "wide_state", "Wide-field state", None, None, None, "mdi:camera"),
    Entity("sensor", "wide_mode", "Wide-field mode", None, None, None, "mdi:camera-iris"),
    Entity("sensor", "wide_gain", "Wide-field gain", None, None, "measurement", "mdi:brightness-6"),
    Entity("binary_sensor", "wide_lp", "Wide-field LP filter", None, None, None, "mdi:image-filter-vintage"),
    Entity("sensor", "active_camera", "Active camera", None, None, None, "mdi:camera-switch"),
    # Stacking
    Entity("sensor", "stack_state", "Stack state", None, None, None, "mdi:layers"),
    Entity("sensor", "stacked_frames", "Stacked frames", None, None, "measurement", "mdi:layers"),
    Entity("sensor", "dropped_frames", "Dropped frames", None, None, "measurement", "mdi:layers-off"),
    Entity("sensor", "total_frames", "Total frames", None, None, "measurement", "mdi:layers-triple"),
    Entity("sensor", "exposure_s", "Exposure", "s", "duration", None, "mdi:camera-timer"),
    Entity("sensor", "integration_min", "Integration time", "min", "duration", "measurement", "mdi:timer-outline"),
    # Plate solve
    Entity("sensor", "ra", "Right ascension", "h", None, "measurement", "mdi:axis-arrow"),
    Entity("sensor", "dec", "Declination", "°", None, "measurement", "mdi:axis-arrow"),
    Entity("sensor", "field_rotation", "Field rotation", "°", None, "measurement", "mdi:rotate-right"),
    Entity("sensor", "focal_length", "Focal length", "mm", None, None, "mdi:image-filter-center-focus"),
    Entity("sensor", "solve_stars", "Stars detected", None, None, "measurement", "mdi:star-four-points"),
    Entity("sensor", "fov", "Field of view", None, None, None, "mdi:overscan"),
    # Plan / scheduler
    Entity("sensor", "plan_name", "Plan", None, None, None, "mdi:clipboard-list"),
    Entity("binary_sensor", "plan_active", "Plan running", None, "running", None, "mdi:play-circle"),
    # Image / AI
    Entity("sensor", "detected_objects", "Objects in frame", None, None, "measurement", "mdi:star-shooting"),
    Entity("sensor", "detected_names", "Catalog objects", None, None, None, "mdi:telescope"),
    Entity("sensor", "last_saved", "Last saved file", None, None, None, "mdi:content-save"),
    Entity("sensor", "filter_position", "Filter position", None, None, "measurement", "mdi:filter-variant"),
    # Pointing / mount / status
    Entity("binary_sensor", "tracking", "Tracking", None, None, None, "mdi:target"),
    Entity("binary_sensor", "slewing", "Slewing", None, "moving", None, "mdi:telescope"),
    Entity("sensor", "goto_state", "Goto state", None, None, None, "mdi:crosshairs-gps"),
    # Alt/Az computed from the plate-solved RA/Dec + the scope's GPS site location,
    # so it stays correct during capture (unlike the Alpaca alt/az, which read 0).
    Entity("sensor", "altitude", "Altitude", "°", None, "measurement", "mdi:angle-acute"),
    Entity("sensor", "azimuth", "Azimuth", "°", None, "measurement", "mdi:compass"),
    Entity("binary_sensor", "at_park", "Parked", None, None, None, "mdi:home-import-outline"),
    Entity("binary_sensor", "at_home", "At home", None, None, None, "mdi:home"),
    Entity("sensor", "mount_mode", "Mount mode", None, None, None, "mdi:axis"),
    Entity("sensor", "last_alert", "Last alert", None, None, None, "mdi:alert-circle"),
    Entity("binary_sensor", "connected", "Connected", None, "connectivity", None, None),
    # Health (mostly best-effort via get_device_state)
    Entity("sensor", "temperature", "Sensor temperature", "°C", "temperature", "measurement", "mdi:thermometer"),
    Entity("sensor", "battery", "Battery", "%", "battery", "measurement", None),
    Entity("sensor", "charger_status", "Charger", None, None, None, "mdi:power-plug"),
    Entity("sensor", "disk_used_pct", "Storage used", "%", None, "measurement", "mdi:micro-sd"),
    Entity("sensor", "focuser", "Focuser position", None, None, "measurement", "mdi:focus-field"),
    Entity("binary_sensor", "dew_heater", "Dew heater", None, None, None, "mdi:heating-coil"),
    Entity("sensor", "firmware", "Firmware", None, None, None, "mdi:chip"),
]


def slug(name: str) -> str:
    """HA-style slug: lowercase, non-alphanumeric runs collapse to a single
    underscore, with leading/trailing underscores stripped.

    e.g. ``slug("Field of view") == "field_of_view"``. Used to derive a stable
    device id from a scope's name.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def device_block(device_id: str, device_name: str) -> dict[str, Any]:
    """The MQTT discovery ``device`` block tying every entity to one HA device.

    ``device_id`` (a slug of the scope name) is the stable identifier; the
    rotating Alpaca ``UniqueID`` is deliberately not used.
    """
    return {
        "identifiers": [device_id],
        "name": device_name,
        "manufacturer": _MANUFACTURER,
        "model": _MODEL,
    }


def discovery_payload(
    entity: Entity,
    *,
    device_block: dict[str, Any],
    base_topic: str,
    bridge_availability_topic: str,
) -> dict[str, Any]:
    """Build the MQTT discovery config for one entity.

    ``device_block`` (from :func:`device_block`) supplies the per-scope device
    identity, and its first identifier is reused as the id-namespace for
    ``unique_id``/``object_id``. ``base_topic`` (e.g. ``seestar/<device_id>``)
    roots the state topic and the per-scope availability topic.
    ``bridge_availability_topic`` is the process-level LWT topic shared by every
    scope; the two are combined in an ``availability`` LIST with
    ``availability_mode: "all"``, so the entity is available only when BOTH the
    bridge is alive AND its scope is reachable. Optional metadata (unit,
    device/state class, icon) is only emitted when the catalog provides it.
    """
    device_id = device_block["identifiers"][0]
    state_topic = f"{base_topic}/{_STATE_SUBTOPIC}"
    scope_availability_topic = f"{base_topic}/{_AVAILABILITY_SUBTOPIC}"

    if entity.component == _COMPONENT_BINARY_SENSOR:
        template = _BINARY_TEMPLATE % entity.key
    else:
        template = _SENSOR_TEMPLATE % (entity.key, entity.key)

    cfg: dict[str, Any] = {
        "name": entity.name,
        "unique_id": f"{device_id}_{entity.key}",
        "object_id": f"{device_id}_{entity.key}",
        "state_topic": state_topic,
        "availability": availability_list(bridge_availability_topic, scope_availability_topic),
        "availability_mode": _AVAILABILITY_MODE_ALL,
        "value_template": template,
        "device": device_block,
    }
    if entity.component == _COMPONENT_BINARY_SENSOR:
        cfg["payload_on"] = _PAYLOAD_ON
        cfg["payload_off"] = _PAYLOAD_OFF
    if entity.unit:
        cfg["unit_of_measurement"] = entity.unit
    if entity.device_class:
        cfg["device_class"] = entity.device_class
    if entity.state_class:
        cfg["state_class"] = entity.state_class
    if entity.icon:
        cfg["icon"] = entity.icon
    return cfg
