"""Lifecycle ownership for the Carlink integration."""

import asyncio

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import async_get_platforms
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
    HomeAssistantError,
)

from .const import DOMAIN, PLATFORMS
from .runtime import CarlinkRuntime, async_create_client, canonical_identity

type CarlinkConfigEntry = ConfigEntry[CarlinkRuntime]


async def async_setup_entry(hass: HomeAssistant, entry: CarlinkConfigEntry) -> bool:
    """Initialize the configured vehicle without submitting commands."""
    # HA skips integration unload for SETUP_ERROR/SETUP_RETRY. Retain partial
    # setup ownership here until both physical work and platforms are drained.
    if hasattr(entry, "runtime_data"):
        if not await _async_cleanup(hass, entry):
            raise ConfigEntryError(
                translation_domain=DOMAIN, translation_key="platform_cleanup_failed"
            )
        del entry.runtime_data
    try:
        client = await async_create_client(
            hass, entry.data["email"], entry.data["password"]
        )
    except ConfigEntryAuthFailed:
        raise
    except HomeAssistantError as error:
        key = error.translation_key
        if key in {"no_assets", "invalid_response"}:
            raise ConfigEntryError(
                translation_domain=DOMAIN, translation_key=key
            ) from None
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN, translation_key="cannot_connect"
        ) from None
    if canonical_identity(client) != (
        str(entry.data["account_id"]),
        str(entry.data["asset_id"]),
    ):
        raise ConfigEntryError(
            translation_domain=DOMAIN, translation_key="identity_mismatch"
        )
    entry.runtime_data = CarlinkRuntime(
        hass, client, on_auth_failed=lambda: entry.async_start_reauth(hass)
    )
    for platform in PLATFORMS:
        # Core shields the platform coroutine itself. Cancelling its parent can
        # otherwise return before a late entity addition. Settle the entire
        # forward (including pending adds) before starting teardown.
        task = asyncio.create_task(_async_forward_platform(hass, entry, platform))
        try:
            failed = await asyncio.shield(task)
        except asyncio.CancelledError:
            await _async_settle(asyncio.create_task(entry.runtime_data.async_close()))
            await _async_settle(task)
            if await async_unload_entry(hass, entry):
                del entry.runtime_data
            raise
        if failed:
            if not await async_unload_entry(hass, entry):
                raise ConfigEntryError(
                    translation_domain=DOMAIN, translation_key="platform_cleanup_failed"
                )
            del entry.runtime_data
            raise ConfigEntryError(
                translation_domain=DOMAIN, translation_key="platform_setup_failed"
            ) from None
    return True


async def _async_forward_platform(
    hass: HomeAssistant, entry: CarlinkConfigEntry, platform: Platform
) -> bool:
    """Record completed forwards and ownership acquired by failed forwards."""
    before = set(async_get_platforms(hass, DOMAIN))
    failed = False
    try:
        await hass.config_entries.async_forward_entry_setups(entry, [platform])
    except Exception:
        failed = True
    # Core's batch forward returns None even when a base component fails to
    # load. Only actual new ownership counts, not a non-raising forward call.
    # Public lookup retains old reset objects, hence the snapshot diff.
    acquired = [
        item
        for item in async_get_platforms(hass, DOMAIN)
        if item not in before and item.config_entry is entry and item.domain == platform
    ]
    if acquired:
        entry.runtime_data.platforms.add(platform)
    # HA 2026.9 registers ownership before setup and swallows platform errors.
    # Its public batch forward exposes no success result. Read (never mutate)
    # Core's completion flag after the retained forward settles; ownership must
    # still be cleaned even when initialization failed after scheduling entities.
    return failed or not acquired or not all(item._setup_complete for item in acquired)


async def _async_settle[T](task: asyncio.Task[T]) -> T:
    """Hold ownership through caller cancellation, never restart the operation."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise


async def _async_cleanup(hass: HomeAssistant, entry: CarlinkConfigEntry) -> bool:
    """Disable and drain before removing platforms; retain ownership on failure."""
    runtime = entry.runtime_data
    if runtime.cleanup_task is None:
        # Not an independently HA-tracked wrapper: its strong owner is runtime.
        # Cancelling the wrapper must not cancel a reset after Core pops it.
        runtime.cleanup_task = asyncio.create_task(_async_teardown(hass, entry))
    try:
        return await asyncio.shield(runtime.cleanup_task)
    except asyncio.CancelledError:
        if runtime.cleanup_task.cancelled():
            # Direct cancellation of the actual operation is irrecoverable.
            # Retain this terminal outcome; never pretend an absent platform
            # means its interrupted reset finished.
            return False
        raise


async def _async_teardown(hass: HomeAssistant, entry: CarlinkConfigEntry) -> bool:
    """Run each acquired platform teardown once, after physical work drains."""
    runtime = entry.runtime_data
    await runtime.async_close()
    success = True
    for platform in PLATFORMS:
        if platform not in runtime.platforms:
            continue
        try:
            unloaded = await hass.config_entries.async_unload_platforms(
                entry, [platform]
            )
        except Exception:
            unloaded = False
        if unloaded:
            runtime.platforms.remove(platform)
        else:
            # False is a genuine failure, not evidence that nothing was loaded.
            # Core may already have popped ownership. Recovery requires restart.
            success = False
    return success


async def async_unload_entry(hass: HomeAssistant, entry: CarlinkConfigEntry) -> bool:
    """Finish the unload transaction even if its caller is cancelled.

    HA leaves UNLOAD_IN_PROGRESS non-recoverable if cancellation escapes this
    hook. Defer cancellation until cleanup finishes and return the real result,
    allowing HA to settle state while it still holds the entry's setup lock.
    """
    task = hass.async_create_task(_async_cleanup(hass, entry), "carlink unload")
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                # Recreate only the waiter, never the retained reset operation.
                task = hass.async_create_task(
                    _async_cleanup(hass, entry), "carlink unload"
                )
