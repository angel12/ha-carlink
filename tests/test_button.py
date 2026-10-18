"""Actual button/lock forwarding and services, never live vehicle traffic."""

import json
from pathlib import Path

import pytest
from homeassistant.helpers import device_registry as dr, entity_registry as er

METHODS = ("lock_doors", "unlock_doors", "start_engine", "stop_engine")


def controls(hass, entry):
    return {
        item.unique_id.rsplit("_", 1)[1]: item
        for item in er.async_entries_for_config_entry(
            er.async_get(hass), entry.entry_id
        )
    }


async def call(hass, entity, service=None):
    domain = entity.entity_id.split(".")[0]
    await hass.services.async_call(
        domain, service or "press", {"entity_id": entity.entity_id}, blocking=True
    )


@pytest.mark.parametrize(
    "action,method", [("start", "start_engine"), ("stop", "stop_engine")]
)
async def test_button_services(hass, platform_entry, platform_client, action, method):
    assert await hass.config_entries.async_setup(platform_entry.entry_id)
    await hass.async_block_till_done()
    entities = controls(hass, platform_entry)
    assert set(entities) == {"lock", "start", "stop"}
    assert len({item.device_id for item in entities.values()}) == 1
    device = dr.async_get(hass).async_get(entities["lock"].device_id)
    assert device.identifiers == {("carlink", '["123", "456"]')}
    for suffix, entity in entities.items():
        assert entity.unique_id == f'["123", "456"]_{suffix}'
        assert entity.domain == ("lock" if suffix == "lock" else "button")
        resolved_name = {
            "lock": "Doors", "start": "Start engine", "stop": "Stop engine"
        }[suffix]
        assert entity.original_name == resolved_name
        assert hass.states.get(entity.entity_id).attributes["friendly_name"] == (
            f"Vehicle {resolved_name}"
        )
        assert hass.states.get(entity.entity_id).state == "unknown"
        assert hass.states.get(entity.entity_id).attributes.get("device_class") is None
    for name in METHODS:
        getattr(platform_client[0], name).assert_not_called()
    await call(hass, entities[action])
    getattr(platform_client[0], method).assert_called_once_with()
    for name in METHODS:
        assert getattr(platform_client[0], name).call_count == int(name == method)
    assert hass.states.get(entities["lock"].entity_id).state == "unknown"
    assert hass.states.async_entity_ids("switch") == []


@pytest.mark.parametrize("domain", ["lock", "button"])
@pytest.mark.parametrize("after_add", [False, True])
async def test_platform_initialization_failure(
    hass, platform_entry, platform_client, domain, after_add
):
    from unittest.mock import patch
    from importlib import import_module
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.helpers.entity_platform import async_get_platforms

    module = import_module(f"custom_components.carlink.{domain}")
    original = module.async_setup_entry
    runtimes = []

    async def fail(hass, entry, add):
        runtimes.append(entry.runtime_data)
        if after_add:
            await original(hass, entry, add)
        raise RuntimeError("synthetic platform initialization failure")

    with patch.object(module, "async_setup_entry", side_effect=fail):
        assert not await hass.config_entries.async_setup(platform_entry.entry_id)
    await hass.async_block_till_done()
    assert platform_entry.state is ConfigEntryState.SETUP_ERROR
    assert platform_entry.error_reason_translation_key == "platform_setup_failed"
    assert not hasattr(platform_entry, "runtime_data")
    assert not runtimes[0].available
    assert not runtimes[0]._listeners
    assert all(not p.entities for p in async_get_platforms(hass, "carlink"))
    assert await hass.config_entries.async_reload(platform_entry.entry_id)
    await hass.async_block_till_done()
    assert set(controls(hass, platform_entry)) == {"lock", "start", "stop"}
    for method in METHODS:
        getattr(platform_client[0], method).assert_not_called()


@pytest.mark.parametrize(
    "action,method",
    [
        ("lock", "lock_doors"),
        ("unlock", "unlock_doors"),
        ("start", "start_engine"),
        ("stop", "stop_engine"),
    ],
)
@pytest.mark.parametrize(
    "outcome,key",
    [
        ("Command Failed", "command_failed"),
        ("unexpected", "unknown_outcome"),
        (TimeoutError("RAW-SECRET"), "unknown_outcome"),
    ],
)
async def test_service_errors_never_retry(
    hass, platform_entry, platform_client, action, method, outcome, key, caplog
):
    from homeassistant.exceptions import HomeAssistantError

    assert await hass.config_entries.async_setup(platform_entry.entry_id)
    await hass.async_block_till_done()
    client = platform_client[0]
    if isinstance(outcome, Exception):
        getattr(client, method).side_effect = outcome
    else:
        getattr(client, method).return_value = outcome
    entities = controls(hass, platform_entry)
    with pytest.raises(HomeAssistantError) as caught:
        await call(
            hass,
            entities["lock" if action == "unlock" else action],
            action if action in ("lock", "unlock") else None,
        )
    assert caught.value.translation_key == key
    await hass.async_block_till_done()
    for name in METHODS:
        assert getattr(client, name).call_count == int(name == method)
    assert hass.states.get(entities["lock"].entity_id).state == "unknown"
    assert "RAW-SECRET" not in caplog.text


async def test_reload_identity_and_no_lifecycle_commands(
    hass, platform_entry, platform_client
):
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.exceptions import HomeAssistantError

    assert await hass.config_entries.async_setup(platform_entry.entry_id)
    await hass.async_block_till_done()
    before = controls(hass, platform_entry)
    old = platform_entry.runtime_data
    assert await hass.config_entries.async_reload(platform_entry.entry_id)
    await hass.async_block_till_done()
    assert controls(hass, platform_entry) == before
    assert platform_entry.runtime_data is not old
    assert not old._listeners
    with pytest.raises(HomeAssistantError) as caught:
        await old.async_command("start")
    assert caught.value.translation_key == "closed"
    assert await hass.config_entries.async_unload(platform_entry.entry_id)
    await hass.async_block_till_done()
    assert platform_entry.state is ConfigEntryState.NOT_LOADED
    assert platform_client[1].call_count == 2
    for method in METHODS:
        getattr(platform_client[0], method).assert_not_called()


@pytest.mark.parametrize(
    "action,method", [("lock", "lock_doors"), ("start", "start_engine")]
)
async def test_auth_service_reauth_real_reload(
    hass, platform_entry, platform_client, action, method, caplog
):
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.exceptions import HomeAssistantError
    from requests import Response
    from requests.exceptions import HTTPError

    assert await hass.config_entries.async_setup(platform_entry.entry_id)
    await hass.async_block_till_done()
    before = controls(hass, platform_entry)
    old = platform_entry.runtime_data
    response = Response()
    response.status_code = 401
    getattr(platform_client[0], method).side_effect = HTTPError(
        "RAW-SECRET", response=response
    )
    with pytest.raises(HomeAssistantError) as caught:
        await call(hass, before[action], "lock" if action == "lock" else None)
    assert caught.value.translation_key == "auth_failed"
    await hass.async_block_till_done()
    for entity in before.values():
        assert hass.states.get(entity.entity_id).state == "unavailable"
        # HA skips unavailable targets with a warning, rather than raising.
        await call(hass, entity, "unlock" if entity.domain == "lock" else None)
    with pytest.raises(HomeAssistantError) as caught:
        await old.async_command("stop")
    assert caught.value.translation_key == "auth_failed"
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    replacement = {
        "email": "updated@example.invalid",
        "password": "NEW-SYNTHETIC-SECRET",
    }
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"], replacement
    )
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert platform_entry.state is ConfigEntryState.LOADED
    assert platform_entry.runtime_data is not old
    assert not old._listeners
    assert controls(hass, platform_entry) == before
    assert dict(platform_entry.data) == {
        **replacement,
        "account_id": "123",
        "asset_id": "456",
    }
    assert hass.config_entries.async_entries("carlink") == [platform_entry]
    assert hass.config_entries.flow.async_progress() == []
    assert platform_client[1].call_count == 3
    for entity in before.values():
        assert hass.states.get(entity.entity_id).state == "unknown"
    for name in METHODS:
        assert getattr(platform_client[0], name).call_count == int(name == method)
    for secret in ("RAW-SECRET", "SYNTHETIC-SECRET", replacement["password"]):
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "active,method", [("lock", "lock_doors"), ("start", "start_engine")]
)
@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_service_overlap_and_reload_drain(
    hass, platform_entry, platform_client, active, method, cancel_caller
):
    import asyncio
    import threading
    from unittest.mock import patch
    from homeassistant.core import HomeAssistant
    from homeassistant.exceptions import HomeAssistantError

    assert await hass.config_entries.async_setup(platform_entry.entry_id)
    await hass.async_block_till_done()
    entities = controls(hass, platform_entry)
    old = platform_entry.runtime_data
    entered = asyncio.Event()
    release = threading.Event()
    closing = asyncio.Event()
    old.async_add_listener(lambda: closing.set() if not old.available else None)

    def work():
        hass.loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return "Success"

    getattr(platform_client[0], method).side_effect = work
    tasks = []
    with patch.object(
        hass,
        "async_add_executor_job",
        HomeAssistant.async_add_executor_job.__get__(hass),
    ):
        command = asyncio.create_task(
            call(hass, entities[active], "lock" if active == "lock" else None)
        )
        tasks.append(command)
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if cancel_caller:
                command.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await command
            for action, service in (
                ("lock", "unlock"),
                ("start", None),
                ("stop", None),
            ):
                with pytest.raises(HomeAssistantError) as caught:
                    await call(hass, entities[action], service)
                assert caught.value.translation_key == "busy"
            assert hass.states.get(entities["lock"].entity_id).state == "unknown"
            reload = asyncio.create_task(
                hass.config_entries.async_reload(platform_entry.entry_id)
            )
            tasks.append(reload)
            await asyncio.wait_for(closing.wait(), 2)
            assert not reload.done()
            assert platform_entry.runtime_data is old
            assert platform_client[1].call_count == 1
            for entity in entities.values():
                assert hass.states.get(entity.entity_id).state == "unavailable"
                # Unavailable targets are filtered before entity dispatch.
                await call(hass, entity, "unlock" if entity.domain == "lock" else None)
            release.set()
            assert await asyncio.wait_for(reload, 2)
            if not cancel_caller:
                await command
            await hass.async_block_till_done()
            assert controls(hass, platform_entry) == entities
            assert platform_entry.runtime_data is not old
            assert platform_client[1].call_count == 2
            for name in METHODS:
                assert getattr(platform_client[0], name).call_count == int(
                    name == method
                )
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await old.async_close()


def test_entity_translations():
    root = Path(__file__).parents[1] / "custom_components/carlink"
    strings = json.loads((root / "strings.json").read_text())
    assert strings == json.loads((root / "translations/en.json").read_text())
    assert strings["entity"]["lock"]["doors"]["name"] == "Doors"
    assert strings["entity"]["button"]["start_engine"]["name"] == "Start engine"
    assert strings["entity"]["button"]["stop_engine"]["name"] == "Stop engine"
