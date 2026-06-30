"""Tests for the entity catalog + MQTT discovery payloads (Task 6).

Per the plan:
- a sensor payload has state_topic / value_template referencing value_json.<key>
- a binary_sensor payload has payload_on / payload_off
- slug("Field of view") == "field_of_view"
- every entity key is unique
"""
from seestar_bridge.entities import (
    ENTITIES,
    Entity,
    device_block,
    discovery_payload,
    slug,
)

# A representative device id/name pair reused across tests, so a single change
# here keeps the assertions consistent (DRY).
DEVICE_ID = "seestar_s30_pro"
DEVICE_NAME = "Seestar S30 Pro"
BASE_TOPIC = f"seestar/{DEVICE_ID}"


def _entity(key):
    """Find the catalog entry for a key (tests address entities by key, not index)."""
    for entity in ENTITIES:
        if entity.key == key:
            return entity
    raise KeyError(key)


def _payload(key):
    block = device_block(DEVICE_ID, DEVICE_NAME)
    return discovery_payload(_entity(key), device_block=block, base_topic=BASE_TOPIC)


def test_slug_normalizes_name():
    assert slug("Field of view") == "field_of_view"


def test_slug_collapses_and_strips_non_alnum():
    # Punctuation and repeated separators collapse to a single underscore; the
    # result is lowercase and has no leading/trailing underscores.
    assert slug("  Seestar S30 Pro!! ") == "seestar_s30_pro"
    assert slug("Alt/Az (deg)") == "alt_az_deg"


def test_every_entity_key_is_unique():
    keys = [entity.key for entity in ENTITIES]
    assert len(keys) == len(set(keys))


def test_entity_is_a_named_tuple_with_the_catalog_fields():
    entity = _entity("telephoto_target")
    assert isinstance(entity, Entity)
    assert entity.component == "sensor"
    assert entity.key == "telephoto_target"
    assert entity.name == "Telephoto target"


def test_sensor_payload_references_value_json_key():
    payload = _payload("fov")
    assert payload["state_topic"] == f"{BASE_TOPIC}/state"
    assert "value_json.fov" in payload["value_template"]
    # A plain sensor is not a binary sensor: no on/off payloads.
    assert "payload_on" not in payload
    assert "payload_off" not in payload


def test_binary_sensor_payload_has_on_off_payloads():
    payload = _payload("tracking")
    assert payload["payload_on"] == "ON"
    assert payload["payload_off"] == "OFF"
    assert "value_json.tracking" in payload["value_template"]


def test_payload_namespaces_unique_id_and_object_id_by_device():
    payload = _payload("telephoto_target")
    assert payload["unique_id"] == f"{DEVICE_ID}_telephoto_target"
    assert payload["object_id"] == f"{DEVICE_ID}_telephoto_target"


def test_payload_carries_optional_metadata_only_when_present():
    # exposure_s has a unit + device_class; telephoto_target has neither.
    exposure = _payload("exposure_s")
    assert exposure["unit_of_measurement"] == "s"
    assert exposure["device_class"] == "duration"

    target = _payload("telephoto_target")
    assert "unit_of_measurement" not in target
    assert "device_class" not in target
    assert "state_class" not in target


def test_payload_availability_topic_is_device_scoped():
    payload = _payload("tracking")
    assert payload["availability_topic"] == f"{BASE_TOPIC}/availability"
    assert payload["payload_available"] == "online"
    assert payload["payload_not_available"] == "offline"


def test_device_block_identifies_the_scope():
    block = device_block(DEVICE_ID, DEVICE_NAME)
    assert block["identifiers"] == [DEVICE_ID]
    assert block["name"] == DEVICE_NAME


def test_payload_embeds_the_device_block():
    payload = _payload("tracking")
    assert payload["device"] == device_block(DEVICE_ID, DEVICE_NAME)
