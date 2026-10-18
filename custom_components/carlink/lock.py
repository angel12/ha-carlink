"""Door commands without inferred physical lock state."""

from typing import Any

from homeassistant.components.lock import LockEntity
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
    """Add the configured vehicle's lock."""
    async_add_entities([CarlinkLock(entry.runtime_data)])


class CarlinkLock(CarlinkEntity, LockEntity):
    """A command acknowledgement is not measured lock state."""

    _attr_is_locked = None
    _attr_translation_key = "doors"

    def __init__(self, runtime: CarlinkRuntime) -> None:
        super().__init__(runtime, "lock")

    async def async_lock(self, **kwargs: Any) -> None:
        """Request locking once."""
        await self._runtime.async_command("lock")

    async def async_unlock(self, **kwargs: Any) -> None:
        """Request unlocking once."""
        await self._runtime.async_command("unlock")
