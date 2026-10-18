"""Offline tests using the real Home Assistant test plugin."""

import pytest

# Import before HA mounts its bundled test config directory, which also contains
# a custom_components package. Keep discovery bound to this repository.
import custom_components  # noqa: F401


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    """Allow Home Assistant to discover this repository's custom integration."""
    yield


@pytest.fixture
def platform_entry(hass):
    """Entry for tests forwarding the actual Carlink platforms."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

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
def platform_client():
    """Fake only the external library; HA lifecycle and services remain real."""
    from unittest.mock import Mock, patch

    client = Mock(account_id=123, asset_id=456)
    for method in ("lock_doors", "unlock_doors", "start_engine", "stop_engine"):
        getattr(client, method).return_value = "Success"
    with patch(
        "custom_components.carlink.runtime.PyCarlink", return_value=client
    ) as factory:
        yield client, factory
