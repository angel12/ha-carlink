"""Stateless engine command buttons."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CarlinkConfigEntry
from .entity import CarlinkEntity
from .runtime import CarlinkRuntime


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CarlinkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add explicit start and stop actions, not a running-state switch."""
    async_add_entities(
        [
            CarlinkCommandButton(entry.runtime_data, "start", "start_engine"),
            CarlinkCommandButton(entry.runtime_data, "stop", "stop_engine"),
        ]
    )


class CarlinkCommandButton(CarlinkEntity, ButtonEntity):
    """Submit one engine action through the shared runtime guard."""

    def __init__(
        self, runtime: CarlinkRuntime, action: str, translation_key: str
    ) -> None:
        super().__init__(runtime, action)
        self._action = action
        self._attr_translation_key = translation_key

    async def async_press(self) -> None:
        """Request the action once without inferring engine state."""
        await self._runtime.async_command(self._action)
