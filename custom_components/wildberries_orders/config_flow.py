"""Config flow for Wildberries Orders."""

from __future__ import annotations

import json
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult

from .api.client import WildberriesOrdersClient
from .api.cookies import load_cookies, parse_cookies_input, user_id_from_cookies
from .api.errors import WildberriesAntibotError, WildberriesAuthError, WildberriesOrdersError
from .const import (
    CONF_COOKIES,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema({vol.Required(CONF_COOKIES): str})

STEP_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required("scan_interval", default=DEFAULT_SCAN_INTERVAL): vol.All(
            vol.Coerce(int),
            vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL),
        ),
    }
)


async def _validate_connection(hass: HomeAssistant, cookies_raw: str) -> dict[str, Any]:
    cookies = await hass.async_add_executor_job(load_cookies, cookies_raw)
    async with WildberriesOrdersClient(cookies) as client:
        return await client.fetch_active_deliveries()


class WildberriesOrdersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Wildberries Orders."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            pasted = (user_input.get(CONF_COOKIES) or "").strip()
            try:
                parsed = await self.hass.async_add_executor_job(parse_cookies_input, pasted)
                cookies_raw = json.dumps(parsed, ensure_ascii=False)
                await self.hass.async_add_executor_job(load_cookies, cookies_raw)

                result = await _validate_connection(self.hass, cookies_raw)
                user = result.get("user") or {}
                unique_id = str(user.get("user_id") or user_id_from_cookies(cookies_raw) or "wb_account")
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()

                title_name = user.get("first_name") or user.get("phone") or "Wildberries"
                return self.async_create_entry(
                    title=f"Wildberries — {title_name}",
                    data={CONF_COOKIES: cookies_raw},
                    options={"scan_interval": DEFAULT_SCAN_INTERVAL},
                )
            except ValueError as err:
                _LOGGER.error("Cookie parse error: %s", err)
                errors["base"] = (
                    "invalid_json"
                    if "format" in str(err).lower() or "json" in str(err).lower()
                    else "missing_auth_cookies"
                )
            except WildberriesAuthError as err:
                _LOGGER.error("Wildberries auth failed during setup: %s", err)
                errors["base"] = "invalid_auth"
            except WildberriesAntibotError as err:
                _LOGGER.error("Wildberries antibot during setup: %s", err)
                errors["base"] = "antibot"
            except (WildberriesOrdersError, OSError) as err:
                _LOGGER.error("Wildberries connection failed during setup: %s", err)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
            description_placeholders={
                "hint": "Cookie-Editor / EditThisCookie → Export → JSON, весь массив целиком",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> WildberriesOrdersOptionsFlow:
        return WildberriesOrdersOptionsFlow()


class WildberriesOrdersOptionsFlow(config_entries.OptionsFlow):
    """Options flow — polling interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                STEP_OPTIONS_SCHEMA,
                {"scan_interval": self.config_entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)},
            ),
        )
