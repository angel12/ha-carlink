"""Exercise Carlink through the real Home Assistant flow manager, offline."""

import ast
import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import TextSelector, TextSelectorType
from pycarlink.PyCarLink import MalformedResponseError, NoAssetsError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from requests import Response
from requests.exceptions import ConnectionError, HTTPError

CREDENTIALS = {"email": "synthetic@example.invalid", "password": "synthetic-secret"}


@pytest.fixture
def client():
    client = Mock(account_id=123, asset_id=456)
    with patch(
        "custom_components.carlink.runtime.PyCarlink", return_value=client
    ) as factory:
        yield client, factory


@pytest.fixture(autouse=True)
def lifecycle():
    # Lifecycle is Task 4; only that boundary is stubbed, not HA's flow manager.
    with patch(
        "custom_components.carlink.async_setup_entry", create=True, return_value=True
    ):
        yield


@pytest.mark.parametrize("account,asset", [(123, 456), ("00123", "00456")])
async def test_create_entry(hass, client, account, asset):
    vehicle, factory = client
    vehicle.account_id, vehicle.asset_id = account, asset
    result = await hass.config_entries.flow.async_init(
        "carlink", context={"source": SOURCE_USER}, data=CREDENTIALS
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.version == 1
    assert entry.unique_id == str(account)
    assert dict(entry.data) == dict(
        CREDENTIALS, account_id=str(account), asset_id=str(asset)
    )
    factory.assert_called_once_with(CREDENTIALS["email"], CREDENTIALS["password"])
    for method in ("lock_doors", "unlock_doors", "start_engine", "stop_engine"):
        getattr(vehicle, method).assert_not_called()
    await hass.async_block_till_done()


@pytest.mark.parametrize("stored,returned", [("123", 123), (123, "123")])
async def test_duplicate_account(hass, client, stored, returned):
    entry = MockConfigEntry(
        domain="carlink",
        unique_id=str(stored),
        data=dict(CREDENTIALS, account_id=stored, asset_id="456"),
    )
    entry.add_to_hass(hass)
    client[0].account_id = returned
    result = await hass.config_entries.flow.async_init(
        "carlink", context={"source": SOURCE_USER}, data=CREDENTIALS
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert hass.config_entries.async_entries("carlink") == [entry]


def unauthorized():
    response = Response()
    response.status_code = 401
    return HTTPError("RAW-SECRET", response=response)


@pytest.mark.parametrize(
    "error,key",
    [
        (unauthorized(), "invalid_auth"),
        (ConnectionError("RAW-SECRET"), "cannot_connect"),
        (NoAssetsError("RAW-SECRET"), "no_assets"),
        (MalformedResponseError("RAW-SECRET"), "invalid_response"),
    ],
)
async def test_login_errors_retry(hass, client, caplog, error, key):
    client[1].side_effect = error
    result = await hass.config_entries.flow.async_init(
        "carlink", context={"source": SOURCE_USER}, data=CREDENTIALS
    )
    assert result["errors"] == {"base": key}
    assert hass.config_entries.async_entries("carlink") == []
    assert_safe_form(result, caplog)
    client[1].side_effect = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], CREDENTIALS
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


def assert_safe_form(result, caplog):
    for marker in result["data_schema"].schema:
        assert marker.default is vol.UNDEFINED
    for secret in ("RAW-SECRET", CREDENTIALS["password"]):
        assert secret not in repr(result)
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "error",
    [
        RuntimeError("RAW-SECRET"),
        HomeAssistantError("RAW-SECRET", translation_key="RAW-SECRET"),
    ],
)
async def test_unexpected_adapter_errors(hass, caplog, error):
    with patch(
        "custom_components.carlink.config_flow.async_create_client", side_effect=error
    ):
        result = await hass.config_entries.flow.async_init(
            "carlink", context={"source": SOURCE_USER}, data=CREDENTIALS
        )
    assert result["errors"] == {"base": "unknown"}
    assert_safe_form(result, caplog)


@pytest.mark.parametrize("stored,returned", [("123", 123), (123, "123")])
@pytest.mark.parametrize(
    "failure,key",
    [
        ("auth", "invalid_auth"),
        ("connect", "cannot_connect"),
        ("assets", "no_assets"),
        ("response", "invalid_response"),
        ("account", "identity_mismatch"),
        ("asset", "identity_mismatch"),
        ("leading_zero", "identity_mismatch"),
        ("whitespace", "identity_mismatch"),
    ],
)
async def test_reauth_preserves_then_updates(
    hass, client, caplog, stored, returned, failure, key
):
    original = dict(CREDENTIALS, account_id=stored, asset_id=stored, extra="preserved")
    entry = MockConfigEntry(domain="carlink", unique_id=str(stored), data=original)
    entry.add_to_hass(hass)
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    flow = hass.config_entries.flow.async_progress()[0]
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    assert result["step_id"] == "reauth_confirm"
    assert_safe_form(result, caplog)
    vehicle, factory = client
    vehicle.account_id = vehicle.asset_id = returned
    failures = {
        "auth": unauthorized(),
        "connect": ConnectionError("RAW-SECRET"),
        "assets": NoAssetsError("RAW-SECRET"),
        "response": MalformedResponseError("RAW-SECRET"),
    }
    if failure in failures:
        factory.side_effect = failures[failure]
    elif failure == "account":
        vehicle.account_id = "different"
    elif failure == "asset":
        vehicle.asset_id = "different"
    elif failure == "leading_zero":
        vehicle.asset_id = "0" + str(stored)
    else:
        vehicle.account_id = " " + str(stored)
    replacement = {"email": "new@example.invalid", "password": "new-synthetic-secret"}
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], replacement
        )
        assert result["errors"] == {"base": key}
        assert dict(entry.data) == original
        assert entry.unique_id == str(stored)
        assert_safe_form(result, caplog)
        assert replacement["password"] not in repr(result) + caplog.text
        reload.assert_not_called()
        factory.side_effect = None
        vehicle.account_id = vehicle.asset_id = returned
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], replacement
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "reauth_successful"
        await hass.async_block_till_done()
        reload.assert_awaited_once_with(entry.entry_id)
    assert dict(entry.data) == {**original, **replacement}
    assert hass.config_entries.async_entries("carlink") == [entry]
    assert hass.config_entries.flow.async_progress() == []
    for method in ("lock_doors", "unlock_doors", "start_engine", "stop_engine"):
        getattr(vehicle, method).assert_not_called()


async def test_command_auth_reauth_does_not_replay(hass, client):
    from custom_components.carlink.runtime import CarlinkRuntime

    entry = MockConfigEntry(
        domain="carlink",
        unique_id="123",
        data=dict(CREDENTIALS, account_id="123", asset_id="456"),
    )
    entry.add_to_hass(hass)
    vehicle, _ = client
    vehicle.start_engine.side_effect = unauthorized()
    runtime = CarlinkRuntime(
        hass, vehicle, on_auth_failed=lambda: entry.async_start_reauth(hass)
    )
    with pytest.raises(HomeAssistantError):
        await runtime.async_command("start")
    await hass.async_block_till_done()
    flow = hass.config_entries.flow.async_progress()[0]
    with patch.object(hass.config_entries, "async_reload", return_value=True) as reload:
        result = await hass.config_entries.flow.async_configure(
            flow["flow_id"], CREDENTIALS
        )
        assert result["reason"] == "reauth_successful"
        await hass.async_block_till_done()
        reload.assert_awaited_once_with(entry.entry_id)
    vehicle.start_engine.assert_called_once_with()
    assert not runtime.available
    await runtime.async_close()


def test_translations_cover_referenced_keys():
    root = Path(__file__).parents[1] / "custom_components/carlink"
    strings = json.loads((root / "strings.json").read_text())
    assert strings == json.loads((root / "translations/en.json").read_text())
    config = strings["config"]
    assert set(config["error"]) >= {
        "invalid_auth",
        "cannot_connect",
        "no_assets",
        "invalid_response",
        "identity_mismatch",
        "unknown",
    }
    assert set(config["abort"]) >= {"already_configured", "reauth_successful"}
    for step in ("user", "reauth_confirm"):
        assert set(config["step"][step]["data"]) == {"email", "password"}
    # Cover literal exception/entity translation keys wherever currently referenced.
    for source in root.glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "translation_key" and isinstance(
                        kw.value, ast.Constant
                    ):
                        assert kw.value.value in strings["exceptions"]
                if isinstance(node.func, ast.Name) and node.func.id == "_error":
                    assert node.args[0].value in strings["exceptions"]
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "_attr_translation_key"
                for t in node.targets
            ):
                assert any(
                    node.value.value in keys for keys in strings["entity"].values()
                )


async def test_initial_form(hass):
    result = await hass.config_entries.flow.async_init(
        "carlink", context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}
    fields = {key.schema: value for key, value in result["data_schema"].schema.items()}
    assert isinstance(fields["password"], TextSelector)
    assert fields["password"].config["type"] == TextSelectorType.PASSWORD
    assert fields["email"].config["type"] == TextSelectorType.EMAIL
