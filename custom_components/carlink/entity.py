"""Shared identity and readiness for command-only Carlink entities."""

import json

from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN
from .runtime import CarlinkRuntime


class CarlinkEntity(Entity):
    """A stable vehicle binding; availability does not imply vehicle reachability."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, runtime: CarlinkRuntime, action: str) -> None:
        self._runtime = runtime
        identifier = json.dumps(list(runtime.identity))
        self._attr_unique_id = f"{identifier}_{action}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, identifier)}, name="Vehicle"
        )

    @property
    def available(self) -> bool:
        """Return cached client readiness, never perform I/O."""
        return self._runtime.available

    async def async_added_to_hass(self) -> None:
        """Subscribe until removed; no polling or entity-owned reauth logic."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._runtime.async_add_listener(self.async_write_ha_state)
        )
