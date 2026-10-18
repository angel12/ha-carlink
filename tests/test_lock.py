"""Real HA lock services and platform forwarding, with only the client faked."""

import pytest
from homeassistant.helpers import entity_registry as er


@pytest.mark.parametrize(
    "service,method", [("lock", "lock_doors"), ("unlock", "unlock_doors")]
)
async def test_lock_service(hass, platform_entry, platform_client, service, method):
    assert await hass.config_entries.async_setup(platform_entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, platform_entry.entry_id)
    assert len(entities) == 3
    entity = next(item for item in entities if item.domain == "lock")
    assert entity.unique_id == '["123", "456"]_lock'
    state = hass.states.get(entity.entity_id)
    assert state.state == "unknown"
    assert state.attributes["supported_features"] == 0
    await hass.services.async_call(
        "lock", service, {"entity_id": entity.entity_id}, blocking=True
    )
    assert hass.states.get(entity.entity_id).state == "unknown"
    for name in ("lock_doors", "unlock_doors", "start_engine", "stop_engine"):
        assert getattr(platform_client[0], name).call_count == int(name == method)
    getattr(platform_client[0], method).assert_called_once_with()
