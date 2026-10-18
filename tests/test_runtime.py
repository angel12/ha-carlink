"""Offline runtime contract tests, using real HA executor jobs."""

import asyncio
import threading
import traceback
from types import SimpleNamespace

from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from requests.exceptions import ConnectionError, HTTPError, Timeout
from requests import Response
from pycarlink.PyCarLink import MalformedResponseError, NoAssetsError
from unittest.mock import Mock

import pytest

from custom_components.carlink import runtime

METHODS = {
    "lock": "lock_doors",
    "unlock": "unlock_doors",
    "start": "start_engine",
    "stop": "stop_engine",
}


@pytest.fixture(autouse=True)
def real_executor(hass, monkeypatch):
    """Bypass the HA test plugin's inline execution of Mock callables."""
    from homeassistant.core import HomeAssistant

    monkeypatch.setattr(
        hass,
        "async_add_executor_job",
        HomeAssistant.async_add_executor_job.__get__(hass),
    )


@pytest.fixture
def client():
    return SimpleNamespace(
        account_id=12,
        asset_id="003",
        **{name: Mock(return_value="Success") for name in METHODS.values()},
    )


@pytest.mark.parametrize("action", METHODS)
async def test_routes_exactly_once(hass, client, action):
    instance = runtime.CarlinkRuntime(hass, client)
    assert instance.identity == ("12", "003")
    assert instance.available
    await instance.async_command(action)
    for candidate, method in METHODS.items():
        assert getattr(client, method).call_count == (candidate == action)


@pytest.mark.parametrize("account,asset", [(12, 34), ("12", "34"), ("012", " 34 ")])
def test_canonical_identity(client, account, asset):
    client.account_id, client.asset_id = account, asset
    assert runtime.canonical_identity(client) == (str(account), str(asset))


@pytest.mark.parametrize("action", METHODS)
@pytest.mark.parametrize(
    "result,key",
    [
        ("Command Failed", "command_failed"),
        ("Command Timed Out", "unknown_outcome"),
        (None, "unknown_outcome"),
        ("SECRET_RESULT", "unknown_outcome"),
    ],
)
async def test_outcomes(hass, client, action, result, key, caplog):
    getattr(client, METHODS[action]).return_value = result
    instance = runtime.CarlinkRuntime(hass, client)
    with pytest.raises(HomeAssistantError) as caught:
        await instance.async_command(action)
    assert caught.value.translation_key == key
    assert "SECRET_RESULT" not in str(caught.value) + caplog.text
    for candidate, method in METHODS.items():
        assert getattr(client, method).call_count == (candidate == action)


async def test_unknown_action_never_submitted(hass, client):
    with pytest.raises(HomeAssistantError) as caught:
        await runtime.CarlinkRuntime(hass, client).async_command("get_asset_status")
    assert caught.value.translation_key == "invalid_action"
    assert not any(getattr(client, method).called for method in METHODS.values())


def http_error(status):
    response = Response()
    response.status_code = status
    return HTTPError("SYNTHETIC_SECRET", response=response)


@pytest.mark.parametrize(
    "error,key",
    [
        (http_error(401), "auth_failed"),
        (http_error(403), "unknown_outcome"),
        (http_error(500), "unknown_outcome"),
        (HTTPError("SYNTHETIC_SECRET"), "unknown_outcome"),
        (ConnectionError("SYNTHETIC_SECRET"), "unknown_outcome"),
        (Timeout("SYNTHETIC_SECRET"), "unknown_outcome"),
        (MalformedResponseError("SYNTHETIC_SECRET"), "unknown_outcome"),
        (RuntimeError("SYNTHETIC_SECRET"), "unknown_outcome"),
    ],
)
async def test_command_exception_safe_and_no_replay(hass, client, error, key, caplog):
    callback = Mock()
    client.lock_doors.side_effect = error
    instance = runtime.CarlinkRuntime(hass, client, on_auth_failed=callback)
    with pytest.raises(HomeAssistantError) as caught:
        await instance.async_command("lock")
    assert caught.value.translation_key == key
    assert "SYNTHETIC_SECRET" not in "".join(traceback.format_exception(caught.value))
    assert "SYNTHETIC_SECRET" not in caplog.text
    callback.assert_called_once_with() if key == "auth_failed" else callback.assert_not_called()
    assert instance.available == (key != "auth_failed")
    client.lock_doors.assert_called_once_with()
    assert not client.unlock_doors.called
    assert not client.start_engine.called
    assert not client.stop_engine.called
    if key != "auth_failed":
        await instance.async_command("stop")


async def test_constructor_executor_and_credentials_unchanged(
    hass, client, monkeypatch
):
    main_thread = threading.get_ident()

    def construct(email, password):
        assert threading.get_ident() != main_thread
        assert (email, password) == (" EMail ", " PassWord ")
        return client

    monkeypatch.setattr(runtime, "PyCarlink", construct)
    assert await runtime.async_create_client(hass, " EMail ", " PassWord ") is client


@pytest.mark.parametrize(
    "error,key,error_type",
    [
        (http_error(401), "auth_failed", ConfigEntryAuthFailed),
        (http_error(403), "cannot_connect", HomeAssistantError),
        (Timeout("SYNTHETIC_SECRET"), "cannot_connect", HomeAssistantError),
        (
            MalformedResponseError("SYNTHETIC_SECRET"),
            "invalid_response",
            HomeAssistantError,
        ),
        (NoAssetsError("SYNTHETIC_SECRET"), "no_assets", HomeAssistantError),
    ],
)
async def test_setup_errors_safe(hass, monkeypatch, error, key, error_type, caplog):
    monkeypatch.setattr(runtime, "PyCarlink", Mock(side_effect=error))
    with pytest.raises(error_type) as caught:
        await runtime.async_create_client(hass, "email", "password")
    assert caught.value.translation_key == key
    assert isinstance(caught.value, ConfigEntryAuthFailed) == (key == "auth_failed")
    assert "SYNTHETIC_SECRET" not in "".join(traceback.format_exception(caught.value))
    assert "SYNTHETIC_SECRET" not in caplog.text


@pytest.fixture
def blocked_worker(client):
    entered, release = threading.Event(), threading.Event()

    def work():
        entered.set()
        assert release.wait(5), "test did not release executor worker"
        return "Success"

    client.lock_doors.side_effect = work
    yield entered, release
    release.set()


async def wait_entered(event):
    async with asyncio.timeout(2):
        while not event.is_set():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("cancel_caller", [False, True])
async def test_worker_guard_until_real_completion(
    hass, client, blocked_worker, cancel_caller
):
    entered, release = blocked_worker
    instance = runtime.CarlinkRuntime(hass, client)
    caller = asyncio.create_task(instance.async_command("lock"))
    try:
        await wait_entered(entered)  # Real worker is blocked; loop still responsive.
        if cancel_caller:
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
        with pytest.raises(HomeAssistantError) as caught:
            await instance.async_command("stop")
        assert caught.value.translation_key == "busy"
        client.stop_engine.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await hass.async_block_till_done()
    await instance.async_command("stop")
    client.stop_engine.assert_called_once_with()


async def test_cancelled_close_keeps_runtime_closed_and_drains(
    hass, client, blocked_worker
):
    entered, release = blocked_worker
    instance = runtime.CarlinkRuntime(hass, client)
    caller = asyncio.create_task(instance.async_command("lock"))
    closer = None
    try:
        await wait_entered(entered)
        closer = asyncio.create_task(instance.async_close())
        await asyncio.sleep(0)
        assert not instance.available
        assert not closer.done()
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closer
        with pytest.raises(HomeAssistantError) as caught:
            await instance.async_command("stop")
        assert caught.value.translation_key == "closed"
        closer = asyncio.create_task(instance.async_close())
        await asyncio.sleep(0)
        assert not closer.done()
    finally:
        release.set()
        await asyncio.gather(
            caller, *([closer] if closer else []), return_exceptions=True
        )
    await instance.async_close()
    assert not instance.available
    client.lock_doors.assert_called_once_with()
    client.stop_engine.assert_not_called()


async def test_auth_invalidated_rejects_new_commands(hass, client):
    client.lock_doors.side_effect = http_error(401)
    instance = runtime.CarlinkRuntime(hass, client)
    with pytest.raises(HomeAssistantError):
        await instance.async_command("lock")
    with pytest.raises(HomeAssistantError) as caught:
        await instance.async_command("stop")
    assert caught.value.translation_key == "auth_failed"
    client.stop_engine.assert_not_called()


async def test_callback_failure_is_sanitized(hass, client, caplog):
    client.lock_doors.side_effect = http_error(401)
    instance = runtime.CarlinkRuntime(
        hass, client, on_auth_failed=Mock(side_effect=RuntimeError("SYNTHETIC_SECRET"))
    )
    with pytest.raises(HomeAssistantError) as caught:
        await instance.async_command("lock")
    assert caught.value.translation_key == "auth_failed"
    assert "SYNTHETIC_SECRET" not in "".join(traceback.format_exception(caught.value))
    assert "SYNTHETIC_SECRET" not in caplog.text
    await instance.async_close()


@pytest.mark.parametrize("failure", [http_error(401), Timeout("SYNTHETIC_SECRET")])
async def test_cancelled_caller_failure_retrieved_and_close_drains(
    hass, client, failure, caplog
):
    entered, release = threading.Event(), threading.Event()
    callback = Mock()

    def work():
        entered.set()
        assert release.wait(5)
        raise failure

    client.lock_doors.side_effect = work
    instance = runtime.CarlinkRuntime(hass, client, on_auth_failed=callback)
    loop_errors = []
    loop = asyncio.get_running_loop()
    old_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda loop, context: loop_errors.append(context))
    caller = asyncio.create_task(instance.async_command("lock"))
    closer = None
    try:
        await wait_entered(entered)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        closer = asyncio.create_task(instance.async_close())
        await asyncio.sleep(0)
        assert not closer.done()
        release.set()
        await closer  # Command failure must not fail draining.
        await hass.async_block_till_done()
        import gc

        gc.collect()
        await asyncio.sleep(0)
        assert not loop_errors, [
            (c.get("message"), str(c.get("exception"))) for c in loop_errors
        ]
        assert "SYNTHETIC_SECRET" not in caplog.text
        assert callback.call_count == isinstance(failure, HTTPError)
        client.lock_doors.assert_called_once_with()
    finally:
        release.set()
        await asyncio.gather(
            caller, *([closer] if closer else []), return_exceptions=True
        )
        loop.set_exception_handler(old_handler)


async def test_internal_cancellation_does_not_release_worker(
    hass, client, blocked_worker
):
    entered, release = blocked_worker
    instance = runtime.CarlinkRuntime(hass, client)
    caller = asyncio.create_task(instance.async_command("lock"))
    try:
        await wait_entered(entered)
        # Simulate shutdown cancellation of the tracked internal owner itself.
        instance._task.cancel()
        await asyncio.sleep(0)
        with pytest.raises(HomeAssistantError) as caught:
            await instance.async_command("stop")
        assert caught.value.translation_key == "busy"
        assert not caller.done()
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await hass.async_block_till_done()
    await instance.async_command("stop")
    client.stop_engine.assert_called_once_with()


@pytest.mark.parametrize("cancel_owner", [False, True])
@pytest.mark.parametrize(
    "failure", [None, http_error(401), Timeout("SYNTHETIC_SECRET")]
)
async def test_tracked_future_cancellation_drains_real_worker(
    hass, client, cancel_owner, failure, caplog
):
    entered, release = threading.Event(), threading.Event()
    callback = Mock()

    def work():
        entered.set()
        assert release.wait(5)
        if failure is not None:
            raise failure
        return "Success"

    client.lock_doors.side_effect = work
    instance = runtime.CarlinkRuntime(hass, client, on_auth_failed=callback)
    caller = asyncio.create_task(instance.async_command("lock"))
    closer = None
    try:
        await wait_entered(entered)
        workers = [task for task in hass._tasks if not isinstance(task, asyncio.Task)]
        assert len(workers) == 1
        workers[0].cancel()
        if cancel_owner:
            instance._task.cancel()
        for _ in range(5):
            await asyncio.sleep(0)
        with pytest.raises(HomeAssistantError) as caught:
            await instance.async_command("stop")
        assert caught.value.translation_key == "busy"
        client.stop_engine.assert_not_called()
        closer = asyncio.create_task(instance.async_close())
        await asyncio.sleep(0)
        assert not closer.done()
        release.set()
        await asyncio.wait_for(closer, 2)
        await asyncio.gather(caller, return_exceptions=True)
        assert callback.call_count == isinstance(failure, HTTPError)
        assert "SYNTHETIC_SECRET" not in caplog.text
        await instance.async_close()
    finally:
        release.set()
        await asyncio.gather(
            caller, *([closer] if closer else []), return_exceptions=True
        )


@pytest.mark.parametrize("close_immediately", [False, True])
async def test_prestart_owner_cancellation_cleans_reservation(
    hass, client, close_immediately
):
    instance = runtime.CarlinkRuntime(hass, client)
    caller = asyncio.create_task(instance.async_command("unlock"))
    owners = []

    def cancel_before_entry():
        owners.append(instance._task)
        instance._task.cancel()

    asyncio.get_running_loop().call_soon(cancel_before_entry)
    closer = None
    if close_immediately:
        # Queued after command reservation and cancellation, before owner entry.
        closer = asyncio.create_task(instance.async_close())
    with pytest.raises(asyncio.CancelledError):
        await caller
    if closer is not None:
        await asyncio.wait_for(closer, 2)
    assert not instance._busy
    assert instance._task is None
    assert not any(getattr(client, method).called for method in METHODS.values())
    if not close_immediately:
        await instance.async_command("stop")
        client.stop_engine.assert_called_once_with()
    await asyncio.wait_for(instance.async_close(), 2)
    await asyncio.wait_for(instance.async_close(), 2)


@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_queued_executor_cancellation_never_waits_for_unstarted_work(
    hass, client, monkeypatch, cancel_owner
):
    from concurrent.futures import ThreadPoolExecutor

    loop = asyncio.get_running_loop()
    entered, release = threading.Event(), threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)

    def occupy_executor():
        entered.set()
        assert release.wait(5)

    blocker = executor.submit(occupy_executor)
    submitted = []
    original_submit = executor.submit

    def submit(*args, **kwargs):
        future = original_submit(*args, **kwargs)
        submitted.append(future)
        return future

    monkeypatch.setattr(executor, "submit", submit)
    original_executor = loop._default_executor
    monkeypatch.setattr(loop, "_default_executor", executor)
    instance = runtime.CarlinkRuntime(hass, client)
    caller = asyncio.create_task(instance.async_command("lock"))
    try:
        await wait_entered(entered)
        async with asyncio.timeout(2):
            while not submitted:
                await asyncio.sleep(0)
        workers = [task for task in hass._tasks if not isinstance(task, asyncio.Task)]
        assert len(workers) == 1
        workers[0].cancel()
        if cancel_owner:
            instance._task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(caller, 2)
        await asyncio.wait_for(instance.async_close(), 2)
        await asyncio.wait_for(instance.async_close(), 2)
        assert not release.is_set()  # Drain did not need the saturated executor.
        assert submitted[0].cancelled()  # Real queued work item never executes.
        assert not instance._busy
        assert not any(getattr(client, method).called for method in METHODS.values())
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        executor.shutdown(wait=True)
        monkeypatch.setattr(loop, "_default_executor", original_executor)
        blocker.result()
    assert not any(getattr(client, method).called for method in METHODS.values())


async def test_stale_owner_cleanup_cannot_release_new_worker(
    hass, client, blocked_worker
):
    entered, release = blocked_worker
    instance = runtime.CarlinkRuntime(hass, client)
    caller = asyncio.create_task(instance.async_command("unlock"))
    owners = []

    def cancel_before_entry():
        owners.append(instance._task)
        instance._task.cancel()

    asyncio.get_running_loop().call_soon(cancel_before_entry)
    with pytest.raises(asyncio.CancelledError):
        await caller
    caller = asyncio.create_task(instance.async_command("lock"))
    try:
        await wait_entered(entered)
        owner = instance._task
        instance._consume_result(owners[0])
        assert instance._task is owner
        with pytest.raises(HomeAssistantError) as caught:
            await instance.async_command("stop")
        assert caught.value.translation_key == "busy"
        client.stop_engine.assert_not_called()
    finally:
        release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await instance.async_close()


async def test_worker_base_exception_still_settles_termination(
    hass, client, caplog, monkeypatch
):
    outcomes = []
    future_type = runtime.Future

    def retain_outcome():
        outcome = future_type()
        outcomes.append(outcome)
        return outcome

    monkeypatch.setattr(runtime, "Future", retain_outcome)
    client.lock_doors.side_effect = asyncio.CancelledError("SYNTHETIC_SECRET")
    instance = runtime.CarlinkRuntime(hass, client)
    caller = asyncio.create_task(instance.async_command("lock"))
    done, _ = await asyncio.wait([caller], timeout=1)
    try:
        assert caller in done, "worker exit must always settle independent completion"
        with pytest.raises(HomeAssistantError) as caught:
            await caller
        assert caught.value.translation_key == "unknown_outcome"
        assert "SYNTHETIC_SECRET" not in caplog.text
        await asyncio.wait_for(instance.async_close(), 2)
    finally:
        # RED cleanup only: the physical method has already raised/exited.
        if not done:
            outcomes[0].set_result("Success")
            await asyncio.gather(caller, return_exceptions=True)


async def test_real_client_constructor_is_entirely_off_loop(hass, monkeypatch):
    import importlib

    module = importlib.import_module("pycarlink.PyCarLink")
    main_thread = threading.get_ident()
    calls = []
    status_fields = (
        "GpsStatus GpsDateTime Latitude Longitude Speed Heading ExternalVoltage "
        "Rssi DoorStatus EngineStatus IgnitionStatus Alarm EngineShutdownDateTime "
        "BypassUpdatedDateTime BypassTemperature Online"
    ).split()

    def get(url, **kwargs):
        assert threading.get_ident() != main_thread
        assert kwargs["timeout"] == (5.0, 30.0)
        calls.append(url)
        if url.endswith("/Access"):
            data = {"APIKey": "synthetic-key", "AccountID": 12}
        elif url.endswith("/Assets"):
            data = [{"ID": "003"}]
        else:
            data = dict.fromkeys(status_fields)
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: data)

    monkeypatch.setattr(module.requests, "get", get)
    post = Mock(side_effect=AssertionError("constructor must not actuate"))
    monkeypatch.setattr(module.requests, "post", post)
    created = await runtime.async_create_client(hass, " EMail ", " PassWord ")
    assert runtime.canonical_identity(created) == ("12", "003")
    assert (created.email, created.password) == (" EMail ", " PassWord ")
    assert len(calls) == 4
    post.assert_not_called()
