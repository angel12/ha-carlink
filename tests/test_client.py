"""Offline diagnostics through the installed library's real polling path."""

import logging
from unittest.mock import Mock, call

import pytest
from pycarlink import PyCarlink as LibraryClient

from custom_components.carlink import runtime

LOGGER = "custom_components.carlink.client"


class HostileString(str):
    """Ensure untrusted subclasses never reach string formatting or methods."""

    def __str__(self):
        raise AssertionError("SYNTHETIC-SECRET")

    def __repr__(self):
        raise AssertionError("SYNTHETIC-SECRET")

    def strip(self, chars=None):
        raise AssertionError("SYNTHETIC-SECRET")


@pytest.fixture
def transport(monkeypatch):
    """Keep public construction, replacing only network-dependent reads."""
    for name, value in (
        ("retrieve_api_key", "SYNTHETIC-API-SECRET"),
        ("retrieve_account_id", "SYNTHETIC-ACCOUNT"),
        ("get_asset_0", "SYNTHETIC-ASSET"),
        ("get_asset_status", {}),
    ):
        monkeypatch.setattr(LibraryClient, name, Mock(return_value=value))
    post = Mock(return_value=Mock(json=Mock(return_value={"ID": "SYNTHETIC-ID"})))
    get = Mock()
    sleep = Mock()
    monkeypatch.setattr("pycarlink.PyCarLink.requests.post", post)
    monkeypatch.setattr("pycarlink.PyCarLink.requests.get", get)
    monkeypatch.setattr("pycarlink.PyCarLink.time.sleep", sleep)
    return post, get, sleep


@pytest.mark.parametrize("action", ["lock", "unlock", "start", "stop"])
@pytest.mark.parametrize(
    "statuses,key,descriptions",
    [
        (["Success"], None, ["exact:Success"]),
        (["Pending", "Success"], None, ["exact:Pending", "exact:Success"]),
        (
            ["Pending", "Processing"],
            "unknown_outcome",
            ["exact:Pending", "exact:Processing"],
        ),
        (["success"], "command_failed", ["case:Success"]),
        ([" Success "], "command_failed", ["whitespace:Success"]),
        (["SYNTHETIC-SECRET"], "command_failed", ["unknown:string:1-32"]),
        ([None], "unknown_outcome", ["read-error"]),
    ],
)
async def test_real_polling_observed_without_extra_calls(
    hass, transport, caplog, action, statuses, key, descriptions
):
    from homeassistant.exceptions import HomeAssistantError

    post, get, sleep = transport
    get.side_effect = [Mock(json=Mock(return_value={"Status": s})) for s in statuses]
    client = await runtime.async_create_client(hass, "synthetic@example.invalid", "SECRET")
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        instance = runtime.CarlinkRuntime(hass, client)
        if key:
            with pytest.raises(HomeAssistantError) as caught:
                await instance.async_command(action)
            assert caught.value.translation_key == key
        else:
            await instance.async_command(action)
    assert [r.getMessage() for r in caplog.records if r.name == LOGGER] == [
        f"Command status: {description}" for description in descriptions
    ]
    assert post.call_count == 1
    assert get.call_count == len(statuses)
    assert sleep.call_args_list == [call(10)] * len(statuses)
    assert "SYNTHETIC" not in caplog.text


@pytest.mark.parametrize(
    "status,description",
    [
        ("Success", "exact:Success"),
        ("Pending", "exact:Pending"),
        ("Processing", "exact:Processing"),
        ("success", "case:Success"),
        (" Success\t", "whitespace:Success"),
        ("\nPROCESSING ", "case+whitespace:Processing"),
        ("", "unknown:string:empty"),
        ("SYNTHETIC-SECRET", "unknown:string:1-32"),
        ("Success\nBearer FAKE", "unknown:string:1-32"),
        ("Authorization: " + "X" * 40, "unknown:string:33-128"),
        ("X" * 128, "unknown:string:33-128"),
        ("X" * 129, "unknown:string:over-128"),
        (" " * 1000 + "Success", "unknown:string:over-128"),
        ("Ｓuccess", "unknown:string:1-32"),
        (None, "unknown:null"),
        (True, "unknown:boolean"),
        (12, "unknown:number"),
        (1.5, "unknown:number"),
        ({"Authorization": "SYNTHETIC-SECRET"}, "unknown:object"),
        (["SYNTHETIC-SECRET"], "unknown:array"),
        pytest.param(HostileString("Success"), "unknown:other", id="hostile-subclass"),
        pytest.param(object(), "unknown:other", id="opaque-object"),
    ],
)
def test_status_allowlist_preserves_value(
    transport, monkeypatch, caplog, status, description
):
    # Non-string values are rejected by today's upstream parser. Exercise this
    # defensive boundary separately without claiming they can reach it today.
    read = Mock(return_value=status)
    monkeypatch.setattr(LibraryClient, "command_status", read)
    client = runtime.PyCarlink("synthetic@example.invalid", "SECRET")
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert client.command_status("SYNTHETIC-ID") is status
    read.assert_called_once_with("SYNTHETIC-ID")
    records = [r for r in caplog.records if r.name == LOGGER]
    assert [r.getMessage() for r in records] == [f"Command status: {description}"]
    assert all(not r.exc_info and not r.stack_info for r in records)
    assert "SYNTHETIC" not in caplog.text


@pytest.mark.parametrize("fails", [False, True])
def test_diagnostic_failure_cannot_change_upstream_outcome(
    transport, monkeypatch, fails
):
    error = RuntimeError("SYNTHETIC-SECRET")
    read = Mock(side_effect=error) if fails else Mock(return_value="Success")
    monkeypatch.setattr(LibraryClient, "command_status", read)
    monkeypatch.setattr(
        "custom_components.carlink.client._LOGGER.debug",
        Mock(side_effect=RuntimeError("SYNTHETIC-LOGGER-SECRET")),
    )
    client = runtime.PyCarlink("synthetic@example.invalid", "SECRET")
    if fails:
        with pytest.raises(RuntimeError) as caught:
            client.command_status("SYNTHETIC-ID")
        assert caught.value is error
    else:
        assert client.command_status("SYNTHETIC-ID") == "Success"
    read.assert_called_once_with("SYNTHETIC-ID")


def test_upstream_exception_is_observed_without_details(transport, monkeypatch, caplog):
    error = RuntimeError("SYNTHETIC-SECRET")
    read = Mock(side_effect=error)
    monkeypatch.setattr(LibraryClient, "command_status", read)
    client = runtime.PyCarlink("synthetic@example.invalid", "SECRET")
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        with pytest.raises(RuntimeError) as caught:
            client.command_status("SYNTHETIC-ID")
    assert caught.value is error
    read.assert_called_once_with("SYNTHETIC-ID")
    records = [r for r in caplog.records if r.name == LOGGER]
    assert [r.getMessage() for r in records] == ["Command status: read-error"]
    assert all(not r.exc_info and not r.stack_info for r in records)
    assert "SYNTHETIC" not in caplog.text


def test_debug_disabled_and_base_class_unchanged(transport, caplog):
    post, get, sleep = transport
    get.return_value = Mock(json=Mock(return_value={"Status": "Success"}))
    original = LibraryClient.command_status
    client = runtime.PyCarlink("synthetic@example.invalid", "SECRET")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert client.lock_doors() == "Success"
    assert not [r for r in caplog.records if r.name == LOGGER]
    assert LibraryClient.command_status is original
    assert post.call_count == get.call_count == 1
    assert sleep.call_args_list == [call(10)]
