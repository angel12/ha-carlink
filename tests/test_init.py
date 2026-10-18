"""Offline lifecycle tests against actual Home Assistant config entries."""

import asyncio
import threading
from unittest.mock import Mock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import Platform
from pycarlink.PyCarLink import MalformedResponseError, NoAssetsError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests import Response
from requests.exceptions import ConnectionError, HTTPError


def unauthorized():
    response = Response()
    response.status_code = 401
    return HTTPError("RAW-SECRET", response=response)


@pytest.fixture
def entry(hass):
    entry = MockConfigEntry(
        domain="carlink",
        unique_id="123",
        data={
            "email": "test@example.invalid",
            "password": "SYNTHETIC-SECRET",
            "account_id": "123",
            "asset_id": "456",
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def boundary(hass):
    vehicle = Mock(account_id=123, asset_id=456)
    platforms = []

    async def acquire(entry, domains):
        platforms.extend(Mock(config_entry=entry, domain=domain) for domain in domains)

    with (
        patch(
            "custom_components.carlink.async_get_platforms",
            side_effect=lambda *_: platforms,
        ),
        patch(
            "custom_components.carlink.runtime.PyCarlink", return_value=vehicle
        ) as factory,
        patch.object(
            hass.config_entries, "async_forward_entry_setups", side_effect=acquire
        ) as forward,
        patch.object(
            hass.config_entries, "async_unload_platforms", return_value=True
        ) as unload,
    ):
        yield vehicle, factory, forward, unload


def assert_no_commands(vehicle):
    for method in ("lock_doors", "unlock_doors", "start_engine", "stop_engine"):
        getattr(vehicle, method).assert_not_called()


@pytest.mark.parametrize("stored,returned", [("123", 123), (123, "123")])
async def test_setup(hass, entry, boundary, stored, returned):
    vehicle, factory, forward, _ = boundary
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "account_id": stored}
    )
    vehicle.account_id = returned
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.identity == ("123", "456")
    assert entry.runtime_data.available
    factory.assert_called_once_with("test@example.invalid", "SYNTHETIC-SECRET")
    from unittest.mock import call

    assert forward.await_args_list == [
        call(entry, [Platform.LOCK]),
        call(entry, [Platform.BUTTON]),
    ]
    assert_no_commands(vehicle)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "different"),
        ("asset_id", "different"),
        ("account_id", "0123"),
        ("asset_id", " 456"),
    ],
)
async def test_identity_guard(hass, entry, boundary, field, value):
    vehicle, _, forward, _ = boundary
    setattr(vehicle, field, value)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == "identity_mismatch"
    assert not hasattr(entry, "runtime_data")
    forward.assert_not_called()
    assert_no_commands(vehicle)


@pytest.mark.parametrize(
    "error,state,key",
    [
        (ConnectionError("RAW-SECRET"), ConfigEntryState.SETUP_RETRY, "cannot_connect"),
        (unauthorized(), ConfigEntryState.SETUP_ERROR, "auth_failed"),
        (NoAssetsError("RAW-SECRET"), ConfigEntryState.SETUP_ERROR, "no_assets"),
        (
            MalformedResponseError("RAW-SECRET"),
            ConfigEntryState.SETUP_ERROR,
            "invalid_response",
        ),
    ],
)
async def test_setup_errors(hass, entry, boundary, caplog, error, state, key):
    vehicle, factory, forward, _ = boundary
    factory.side_effect = error
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is state
    assert entry.error_reason_translation_key == key
    assert not hasattr(entry, "runtime_data")
    forward.assert_not_called()
    assert_no_commands(vehicle)
    assert "RAW-SECRET" not in caplog.text
    assert "SYNTHETIC-SECRET" not in caplog.text
    assert bool(hass.config_entries.flow.async_progress()) == (key == "auth_failed")
    entry.async_cancel_retry_setup()


async def test_unload_and_reload(hass, entry, boundary):
    vehicle, factory, _, unload = boundary
    assert await hass.config_entries.async_setup(entry.entry_id)
    old = entry.runtime_data
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not old
    assert not old.available
    assert factory.call_count == 2
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hasattr(entry, "runtime_data")
    assert unload.await_count == 4
    assert_no_commands(vehicle)


@pytest.mark.parametrize("cleanup", [True, False, RuntimeError("RAW-SECRET")])
async def test_forward_failure_cleanup(hass, entry, boundary, caplog, cleanup):
    vehicle, factory, forward, unload = boundary
    runtimes = []

    acquire = forward.side_effect

    async def fail(entry, domains):
        if domains == [Platform.LOCK]:
            return await acquire(entry, domains)
        runtimes.append(entry.runtime_data)
        raise RuntimeError("RAW-SECRET")

    forward.side_effect = fail
    if isinstance(cleanup, Exception):
        unload.side_effect = cleanup
    else:
        unload.return_value = cleanup
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert not runtimes[0].available
    unload.assert_awaited_once_with(entry, [Platform.LOCK])
    assert hasattr(entry, "runtime_data") == (cleanup is not True)
    if cleanup is not True:
        assert entry.error_reason_translation_key == "platform_cleanup_failed"
        assert entry.error_reason_translation_domain == "carlink"
    assert "RAW-SECRET" not in caplog.text
    # A failed actual teardown is terminal, unlike an absent platform. Core
    # may already have popped ownership; do not recreate its reset on reload.
    unload.side_effect = None
    unload.return_value = True
    forward.side_effect = acquire
    if cleanup is not True:
        assert not await hass.config_entries.async_reload(entry.entry_id)
        assert factory.call_count == 1
        assert entry.error_reason_translation_key == "platform_cleanup_failed"
        assert entry.error_reason_translation_domain == "carlink"
        from homeassistant.helpers.translation import async_get_translations

        translations = await async_get_translations(hass, "en", "exceptions", {"carlink"})
        message = translations[
            "component.carlink.exceptions.platform_cleanup_failed.message"
        ]
        assert "restart Home Assistant" in message
        # HA's exception renderer removes trailing periods from translations.
        assert message.rstrip(".") in caplog.text
        assert unload.await_count == 1
    else:
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data is not runtimes[0]
    assert_no_commands(vehicle)


@pytest.mark.parametrize("failure", [False, RuntimeError("RAW-SECRET")])
async def test_unload_failure_retains_disabled_runtime(
    hass, entry, boundary, caplog, failure
):
    from custom_components.carlink import async_unload_entry

    vehicle, _, _, unload = boundary
    assert await hass.config_entries.async_setup(entry.entry_id)
    old = entry.runtime_data
    if isinstance(failure, Exception):
        unload.side_effect = failure
    else:
        unload.return_value = failure
    assert not await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    assert entry.runtime_data is old
    assert not old.available
    with pytest.raises(HomeAssistantError):
        await old.async_command("start")
    assert "RAW-SECRET" not in caplog.text
    # Both Core FAILED_UNLOAD and a failed owned teardown require restart.
    calls = unload.await_count
    unload.side_effect = None
    unload.return_value = True
    assert not await async_unload_entry(hass, entry)
    assert unload.await_count == calls
    assert_no_commands(vehicle)


@pytest.fixture
def real_executor(hass):
    with patch.object(
        hass,
        "async_add_executor_job",
        HomeAssistant.async_add_executor_job.__get__(hass),
    ):
        yield


@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("cancel_worker_owners", [False, True])
async def test_live_worker_reload(
    hass, entry, boundary, real_executor, interrupt, cancel_worker_owners
):
    vehicle, factory, _, unload_platforms = boundary
    assert await hass.config_entries.async_setup(entry.entry_id)
    old = entry.runtime_data
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    tracked = []
    executor = hass.async_add_executor_job

    def capture(*args):
        future = executor(*args)
        tracked.append(future)
        return future

    def work():
        hass.loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(5)
            return "Success"
        finally:
            finished.set()

    vehicle.start_engine.side_effect = work
    command = asyncio.create_task(old.async_command("start"))
    tasks = [command]
    try:
        with patch.object(hass, "async_add_executor_job", side_effect=capture):
            await asyncio.wait_for(started.wait(), 2)
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command
        if cancel_worker_owners:
            tracked[0].cancel()
            old._task.cancel()
        if interrupt:
            unloading = asyncio.create_task(
                hass.config_entries.async_unload(entry.entry_id)
            )
            tasks.append(unloading)
            for _ in range(10):
                await asyncio.sleep(0)
            assert not old.available
            unloading.cancel()
            await asyncio.sleep(0)
            unloading.cancel()
        reloading = asyncio.create_task(
            hass.config_entries.async_reload(entry.entry_id)
        )
        tasks.append(reloading)
        for _ in range(10):
            await asyncio.sleep(0)
        assert not finished.is_set()
        assert not reloading.done()
        assert entry.runtime_data is old
        assert factory.call_count == 1
        unload_platforms.assert_not_called()
        with pytest.raises(HomeAssistantError):
            await old.async_command("stop")
        vehicle.stop_engine.assert_not_called()
        release.set()
        if interrupt:
            assert await asyncio.wait_for(unloading, 2)
        assert await asyncio.wait_for(reloading, 2)
        assert finished.is_set()
        assert entry.state is ConfigEntryState.LOADED
        assert entry.runtime_data is not old
        assert entry.runtime_data.available
        assert factory.call_count == 2
        vehicle.start_engine.assert_called_once_with()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await old.async_close()


@pytest.mark.parametrize("cancel_cleanup", [False, True])
async def test_cancelled_platform_unload_finishes_transaction(
    hass, entry, boundary, cancel_cleanup
):
    vehicle, _, _, unload = boundary
    assert await hass.config_entries.async_setup(entry.entry_id)
    entered = asyncio.Event()
    release = asyncio.Event()
    cleanup_tasks = []
    original_create = hass.async_create_task

    def capture(coro, name=None, **kwargs):
        task = original_create(coro, name, **kwargs)
        if name == "carlink unload":
            cleanup_tasks.append(task)
        return task

    async def hold(*args):
        entered.set()
        await release.wait()
        return True

    unload.side_effect = hold
    with patch.object(hass, "async_create_task", side_effect=capture):
        unloading = asyncio.create_task(
            hass.config_entries.async_unload(entry.entry_id)
        )
        try:
            await asyncio.wait_for(entered.wait(), 2)
            unloading.cancel()
            if cancel_cleanup:
                cleanup_tasks[0].cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            assert not unloading.done()
            assert not entry.runtime_data.available
            release.set()
            assert await asyncio.wait_for(unloading, 2)
        finally:
            release.set()
            await asyncio.gather(unloading, return_exceptions=True)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert not hasattr(entry, "runtime_data")
    assert_no_commands(vehicle)


async def test_setup_constructor_runs_off_loop(hass, entry, boundary, real_executor):
    vehicle, factory, _, _ = boundary
    threads = []

    def construct(*args):
        threads.append(threading.get_ident())
        return vehicle

    factory.side_effect = construct
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert len(threads) == 1
    assert threads[0] != threading.get_ident()
    assert_no_commands(vehicle)


async def test_listener_failure_cannot_prevent_reauth_or_drain(
    hass, entry, boundary, caplog
):
    vehicle, _, _, _ = boundary
    assert await hass.config_entries.async_setup(entry.entry_id)
    runtime = entry.runtime_data
    broken = Mock(side_effect=RuntimeError("RAW-SECRET"))
    listener = Mock()
    remove = runtime.async_add_listener(listener)
    runtime.async_add_listener(broken)
    vehicle.start_engine.side_effect = unauthorized()
    with pytest.raises(HomeAssistantError):
        await runtime.async_command("start")
    await hass.async_block_till_done()
    assert not runtime.available
    assert len(hass.config_entries.flow.async_progress()) == 1
    listener.assert_called_once_with()
    remove()
    remove()
    await runtime.async_close()
    listener.assert_called_once_with()
    assert "RAW-SECRET" not in caplog.text


async def test_auth_reauth_reload_no_replay(
    hass, entry, boundary, real_executor, caplog
):
    vehicle, factory, _, _ = boundary
    assert await hass.config_entries.async_setup(entry.entry_id)
    old = entry.runtime_data
    vehicle.start_engine.side_effect = unauthorized()
    with pytest.raises(HomeAssistantError):
        await old.async_command("start")
    await hass.async_block_till_done()
    assert not old.available
    flows = hass.config_entries.flow.async_progress()
    assert len(flows) == 1
    result = await hass.config_entries.flow.async_configure(
        flows[0]["flow_id"],
        {
            "email": "updated@example.invalid",
            "password": "NEW-SYNTHETIC-SECRET",
        },
    )
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not old
    assert entry.runtime_data.available
    assert entry.data["password"] == "NEW-SYNTHETIC-SECRET"
    assert factory.call_count == 3  # initial, reauth validation, actual reload
    assert hass.config_entries.flow.async_progress() == []
    vehicle.start_engine.assert_called_once_with()
    for method in ("lock_doors", "unlock_doors", "stop_engine"):
        getattr(vehicle, method).assert_not_called()
    for secret in ("RAW-SECRET", "NEW-SYNTHETIC-SECRET", "SYNTHETIC-SECRET"):
        assert secret not in caplog.text


@pytest.mark.parametrize("invalidate", ["auth", "close"])
async def test_base_entity_state_notifications(hass, entry, boundary, invalidate):
    import json
    import logging
    from homeassistant.helpers.entity_component import EntityComponent
    from custom_components.carlink.entity import CarlinkEntity

    vehicle, _, _, _ = boundary
    assert await hass.config_entries.async_setup(entry.entry_id)
    runtime = entry.runtime_data
    entity = CarlinkEntity(runtime, "start")
    same_vehicle = CarlinkEntity(runtime, "stop")
    identifier = json.dumps(["123", "456"])
    assert entity.unique_id == identifier + "_start"
    assert same_vehicle.unique_id == identifier + "_stop"
    assert (
        entity.device_info
        == same_vehicle.device_info
        == {
            "identifiers": {("carlink", identifier)},
            "name": "Vehicle",
        }
    )
    assert not entity.should_poll
    assert entity.available
    component = EntityComponent(logging.getLogger(__name__), "test", hass)
    component._platforms["test"].config_entry = entry
    await component.async_add_entities([entity, same_vehicle])
    assert hass.states.get(entity.entity_id).state == "unknown"
    with patch.object(
        entity, "_async_write_ha_state", wraps=entity._async_write_ha_state
    ) as write:
        if invalidate == "auth":
            vehicle.start_engine.side_effect = unauthorized()
            with pytest.raises(HomeAssistantError):
                await runtime.async_command("start")
        else:
            await runtime.async_close()
        assert not entity.available
        assert hass.states.get(entity.entity_id).state == "unavailable"
        assert hass.states.get(same_vehicle.entity_id).state == "unavailable"
        assert write.call_count == 1
        await entity.async_remove()
        await runtime.async_close()
        assert write.call_count == 1
    await same_vehicle.async_remove()


async def test_cancelled_setup_drains_before_retry(
    hass, entry, boundary, real_executor
):
    vehicle, factory, forward, unload = boundary
    forwarding = asyncio.Event()
    forward_release = asyncio.Event()
    acquire = forward.side_effect
    started = asyncio.Event()
    release = threading.Event()

    async def hold_forward(*args):
        forwarding.set()
        await forward_release.wait()
        await acquire(*args)

    def work():
        hass.loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return "Success"

    forward.side_effect = hold_forward
    vehicle.start_engine.side_effect = work
    setup = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
    tasks = [setup]
    try:
        await asyncio.wait_for(forwarding.wait(), 2)
        old = entry.runtime_data
        command = asyncio.create_task(old.async_command("start"))
        tasks.append(command)
        await asyncio.wait_for(started.wait(), 2)
        setup.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        assert not old.available
        assert not setup.done()
        forward.side_effect = acquire
        forward_release.set()
        reload = asyncio.create_task(hass.config_entries.async_reload(entry.entry_id))
        tasks.append(reload)
        for _ in range(10):
            await asyncio.sleep(0)
        assert factory.call_count == 1
        assert entry.runtime_data is old
        assert not reload.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await setup
        assert await asyncio.wait_for(reload, 2)
        await command
        assert entry.runtime_data is not old
        assert entry.state is ConfigEntryState.LOADED
        vehicle.start_engine.assert_called_once_with()
    finally:
        release.set()
        forward_release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture
def real_platforms(hass, entry):
    """Synthetic platform modules, real Core setup/add/reset/unload machinery."""
    import logging
    from types import SimpleNamespace
    from homeassistant.helpers.entity_component import EntityComponent
    from custom_components.carlink.entity import CarlinkEntity

    components = {
        domain: EntityComponent(logging.getLogger(__name__), domain, hass)
        for domain in ("lock", "button")
    }
    entities = []
    before_setup = {}
    before_remove = {}
    before_add = {}
    unload_calls = []

    class TestEntity(CarlinkEntity):
        async def async_added_to_hass(self):
            if gate := before_add.get(self.platform.domain):
                gate[0].set()
                await gate[1].wait()
            await super().async_added_to_hass()

        async def async_will_remove_from_hass(self):
            if gate := before_remove.get(self.platform.domain):
                gate[0].set()
                await gate[1].wait()
            await super().async_will_remove_from_hass()

    async def prepare(hass, config, domain, integration):
        if domain == "button" and before_setup.get("fail"):
            raise RuntimeError("synthetic setup failure before platform ownership")

        async def setup(hass, entry, add):
            if gate := before_setup.get(domain):
                gate[0].set()
                await gate[1].wait()
            entity = TestEntity(entry.runtime_data, domain)
            entities.append(entity)
            add([entity])

        return SimpleNamespace(async_setup_entry=setup)

    async def forward(entry, domains):
        for domain in domains:
            assert await components[domain].async_setup_entry(entry)
            if domain == "button" and before_setup.get("fail_after"):
                raise RuntimeError("synthetic failure after platform ownership")

    async def unload(entry, domain):
        unload_calls.append(domain)
        try:
            return await components[domain].async_unload_entry(entry)
        except Exception:
            # ConfigEntry.async_unload converts component exceptions to False.
            return False

    vehicle = Mock(account_id=123, asset_id=456)
    with (
        patch("custom_components.carlink.runtime.PyCarlink", return_value=vehicle),
        patch(
            "homeassistant.helpers.entity_component.async_prepare_setup_platform",
            side_effect=prepare,
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_setups", side_effect=forward
        ),
        patch.object(
            hass.config_entries, "async_forward_entry_unload", side_effect=unload
        ),
    ):
        yield SimpleNamespace(
            components=components,
            entities=entities,
            setup=before_setup,
            remove=before_remove,
            add=before_add,
            unload_calls=unload_calls,
            vehicle=vehicle,
        )


@pytest.mark.parametrize("failure", ["fail", "fail_after"])
async def test_real_partial_setup_cleanup_and_retry(
    hass, entry, real_platforms, failure
):
    real_platforms.setup[failure] = True
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert not hasattr(entry, "runtime_data")
    expected = ["lock"] if failure == "fail" else ["lock", "button"]
    assert real_platforms.unload_calls == expected
    assert all(
        not component.entities for component in real_platforms.components.values()
    )
    assert all(not entity.platform.entities for entity in real_platforms.entities)
    assert all(
        hass.states.get(entity.entity_id).attributes.get("restored")
        for entity in real_platforms.entities
    )
    real_platforms.setup.clear()
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert_no_commands(real_platforms.vehicle)


async def test_real_reset_survives_cleanup_cancellation(hass, entry, real_platforms):
    assert await hass.config_entries.async_setup(entry.entry_id)
    runtime = entry.runtime_data
    entered, release = asyncio.Event(), asyncio.Event()
    real_platforms.remove["lock"] = (entered, release)
    cleanup_tasks = []
    create = hass.async_create_task

    def capture(coro, name=None, **kwargs):
        task = create(coro, name, **kwargs)
        if name == "carlink unload":
            cleanup_tasks.append(task)
        return task

    with patch.object(hass, "async_create_task", side_effect=capture):
        owner = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert entry.entry_id not in real_platforms.components["lock"]._platforms
            owner.cancel()
            cleanup_tasks[0].cancel()
            for _ in range(10):
                await asyncio.sleep(0)
            assert not owner.done()
            release.set()
            assert await asyncio.wait_for(owner, 2)
        finally:
            release.set()
            await asyncio.gather(owner, return_exceptions=True)
    assert real_platforms.unload_calls.count("lock") == 1
    assert not runtime._listeners
    assert all(not entity.platform.entities for entity in real_platforms.entities)
    assert all(
        hass.states.get(entity.entity_id).attributes.get("restored")
        for entity in real_platforms.entities
    )
    assert entry.state is ConfigEntryState.NOT_LOADED


@pytest.mark.parametrize("defer_cleanup", [False, True])
@pytest.mark.parametrize("phase", ["setup", "add"])
async def test_real_cancelled_setup_settles_late_addition(
    hass, entry, real_platforms, phase, defer_cleanup
):
    entered, release = asyncio.Event(), asyncio.Event()
    getattr(real_platforms, phase)["lock"] = (entered, release)
    owner = asyncio.create_task(hass.config_entries.async_setup(entry.entry_id))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        runtime = entry.runtime_data
        owner.cancel()
        for _ in range(10):
            await asyncio.sleep(0)
        assert not runtime.available
        assert not owner.done()
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        if defer_cleanup:
            # Model retained but still cleanable ownership, not a failed reset.
            # The actual teardown has not run and its terminal result is unset.
            with patch("custom_components.carlink.async_unload_entry", return_value=False):
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await owner
            assert entry.runtime_data is runtime
            assert runtime.cleanup_task is None
            assert runtime.platforms == {Platform.LOCK}
            assert real_platforms.entities[0].platform.entities
            assert real_platforms.unload_calls == []

            # Retry must remove old ownership before constructing its replacement.
            def construct(*args):
                assert not hasattr(entry, "runtime_data")
                assert not runtime.available
                assert not runtime.platforms
                assert not runtime._listeners
                assert not real_platforms.entities[0].platform.entities
                assert real_platforms.unload_calls == [Platform.LOCK]
                return real_platforms.vehicle

            with patch(
                "custom_components.carlink.runtime.PyCarlink", side_effect=construct
            ) as factory:
                assert await hass.config_entries.async_reload(entry.entry_id)
            factory.assert_called_once()
            assert entry.runtime_data is not runtime
            assert entry.runtime_data.available
            assert entry.state is ConfigEntryState.LOADED
            assert await hass.config_entries.async_unload(entry.entry_id)
            assert_no_commands(real_platforms.vehicle)
            return
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        await hass.async_block_till_done()
        assert not runtime._listeners
        assert all(not entity.platform.entities for entity in real_platforms.entities)
        assert all(
            hass.states.get(entity.entity_id).attributes.get("restored")
            for entity in real_platforms.entities
        )
        assert not hasattr(entry, "runtime_data")
        real_platforms.setup.clear()
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert await hass.config_entries.async_unload(entry.entry_id)
    finally:
        release.set()
        await asyncio.gather(owner, return_exceptions=True)


async def test_real_failed_reset_is_terminal(hass, entry, real_platforms):
    from custom_components.carlink import async_unload_entry

    assert await hass.config_entries.async_setup(entry.entry_id)
    runtime = entry.runtime_data
    platform = real_platforms.entities[0].platform
    reset = platform.async_reset

    async def fail():
        await reset()
        raise RuntimeError("synthetic reset failure after ownership was popped")

    with patch.object(platform, "async_reset", side_effect=fail):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    calls = list(real_platforms.unload_calls)
    assert not await async_unload_entry(hass, entry)
    assert real_platforms.unload_calls == calls
    assert runtime.platforms == {"lock"}
    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    assert not runtime.available
    assert not runtime._listeners
    assert_no_commands(real_platforms.vehicle)


async def test_no_ownership_is_not_successful_forward(hass, entry, boundary):
    _, _, forward, unload = boundary
    forward.side_effect = None  # Core can return None despite failed base setup.
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert not hasattr(entry, "runtime_data")
    unload.assert_not_called()
