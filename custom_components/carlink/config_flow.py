"""UI configuration for Carlink."""

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import DOMAIN
from .runtime import async_create_client, canonical_identity

CREDENTIAL_SCHEMA = vol.Schema(
    {
        vol.Required("email"): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL)
        ),
        vol.Required("password"): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class CarlinkConfigFlow(ConfigFlow, domain=DOMAIN):
    """Validate credentials before binding an account and its first vehicle."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Show the service login form."""
        errors = {}
        if user_input is not None:
            identity, error = await self._async_validate(user_input)
            if error:
                errors["base"] = error
            else:
                account_id, asset_id = identity
                await self.async_set_unique_id(account_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input["email"],
                    data={**user_input, "account_id": account_id, "asset_id": asset_id},
                )
        return self.async_show_form(
            step_id="user", data_schema=CREDENTIAL_SCHEMA, errors=errors
        )

    async def async_step_reauth(self, entry_data):
        """Ask for new credentials without echoing the stored password."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        """Replace credentials only for the originally bound account and vehicle."""
        errors = {}
        if user_input is not None:
            identity, error = await self._async_validate(user_input)
            if error:
                errors["base"] = error
            else:
                entry = self._get_reauth_entry()
                if identity != (
                    str(entry.data["account_id"]),
                    str(entry.data["asset_id"]),
                ):
                    errors["base"] = "identity_mismatch"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            "email": user_input["email"],
                            "password": user_input["password"],
                        },
                    )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=CREDENTIAL_SCHEMA, errors=errors
        )

    async def _async_validate(self, user_input):
        """Return only canonical identity or a fixed, safe form error."""
        try:
            client = await async_create_client(
                self.hass, user_input["email"], user_input["password"]
            )
            return canonical_identity(client), None
        except ConfigEntryAuthFailed:
            return None, "invalid_auth"
        except HomeAssistantError as error:
            key = error.translation_key
            if key in {"cannot_connect", "no_assets", "invalid_response"}:
                return None, key
            return None, "unknown"
        except Exception:
            return None, "unknown"
