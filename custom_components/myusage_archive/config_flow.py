"""Config flow: account credentials, re-authentication, options."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .const import (
    CONF_EMAIL,
    CONF_FETCH_TIME,
    CONF_JITTER_MINUTES,
    CONF_KEEP_RAW_PAGES,
    CONF_PASSWORD,
    DEFAULT_FETCH_TIME,
    DEFAULT_JITTER_MINUTES,
    DEFAULT_KEEP_RAW_PAGES,
    DOMAIN,
)
from .vendor.myusage_archive.client import MyUsageClient
from .vendor.myusage_archive.exceptions import (
    AuthenticationError,
    MfaRequiredError,
    MyUsageError,
    UnsupportedAccountError,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_EMAIL): str, vol.Required(CONF_PASSWORD): str}
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


async def _validate_login(hass: HomeAssistant, email: str, password: str) -> str | None:
    """Try the credentials; return an error key or None on success."""
    session = async_create_clientsession(hass, cookie_jar=aiohttp.CookieJar())
    try:
        await MyUsageClient(email, password, session).login()
    except (AuthenticationError, MfaRequiredError):
        return "invalid_auth"
    except UnsupportedAccountError:
        return "unsupported_account"
    except MyUsageError:
        return "cannot_connect"
    except Exception:  # noqa: BLE001 - surfaced as 'unknown', never swallowed silently
        _LOGGER.exception("unexpected error validating MyUsage login")
        return "unknown"
    finally:
        await session.close()
    return None


class MyUsageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Email + password, validated against the live portal."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.casefold())
            self._abort_if_unique_id_configured()
            error = await _validate_login(self.hass, email, user_input[CONF_PASSWORD])
            if error is None:
                return self.async_create_entry(
                    title=email, data={CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        email: str = entry.data[CONF_EMAIL]
        errors: dict[str, str] = {}
        if user_input is not None:
            error = await _validate_login(self.hass, email, user_input[CONF_PASSWORD])
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data={**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": email},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> MyUsageOptionsFlow:
        return MyUsageOptionsFlow()


class MyUsageOptionsFlow(OptionsFlowWithReload):
    """Fetch time (Eastern), jitter, raw-page retention. Reloads on save."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not _TIME_RE.match(str(user_input.get(CONF_FETCH_TIME, ""))):
                errors[CONF_FETCH_TIME] = "invalid_time"
            else:
                return self.async_create_entry(data=user_input)
        current = {
            CONF_FETCH_TIME: self.config_entry.options.get(CONF_FETCH_TIME, DEFAULT_FETCH_TIME),
            CONF_JITTER_MINUTES: self.config_entry.options.get(
                CONF_JITTER_MINUTES, DEFAULT_JITTER_MINUTES
            ),
            CONF_KEEP_RAW_PAGES: self.config_entry.options.get(
                CONF_KEEP_RAW_PAGES, DEFAULT_KEEP_RAW_PAGES
            ),
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_FETCH_TIME): str,
                vol.Required(CONF_JITTER_MINUTES): vol.All(int, vol.Range(min=0, max=180)),
                vol.Required(CONF_KEEP_RAW_PAGES): vol.All(int, vol.Range(min=0, max=30)),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or current),
            errors=errors,
        )
