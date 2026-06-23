"""Config flow for Wildberries Orders."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .api.client import WildberriesOrdersClient
from .api.cookies import load_cookies, user_id_from_cookies
from .api.errors import WildberriesAntibotError, WildberriesAuthError, WildberriesOrdersError
from .api.phone_login_jobs import run_confirm_code, run_send_code
from .const import (
    CONF_COOKIES,
    CONF_PHONE,
    CONF_PHONE_CODE,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


def _phone_login_supported() -> bool:
    """Playwright is optional — HA cannot pip-install it on some ARM/Python builds."""
    try:
        import importlib.util

        return importlib.util.find_spec("playwright") is not None
    except Exception:
        return False


STEP_PHONE_SCHEMA = vol.Schema({vol.Required(CONF_PHONE): str})
STEP_PHONE_CODE_SCHEMA = vol.Schema({vol.Required(CONF_PHONE_CODE): str})
STEP_COOKIES_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_COOKIES): selector.TextSelector(
            selector.TextSelectorConfig(
                type=selector.TextSelectorType.TEXT,
                multiline=True,
            )
        ),
    }
)

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


def _pending_key(flow_id: str) -> str:
    return f"phone_login_{flow_id}"


def _map_setup_error(err: Exception) -> str:
    if isinstance(err, ValueError):
        return "invalid_auth"
    if isinstance(err, WildberriesAuthError):
        return "invalid_auth"
    if isinstance(err, WildberriesAntibotError):
        return "cannot_connect"
    if isinstance(err, WildberriesOrdersError):
        return "cannot_connect"
    return "phone_login_failed"


class WildberriesOrdersConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Wildberries Orders."""

    VERSION = 1

    def __init__(self) -> None:
        self._phone_confirmation: str | None = None
        self._reauth = False

    def _save_pending(self, data: dict[str, Any]) -> None:
        self.hass.data.setdefault(DOMAIN, {})[_pending_key(self.flow_id)] = data

    def _load_pending(self) -> dict[str, Any] | None:
        return self.hass.data.get(DOMAIN, {}).get(_pending_key(self.flow_id))

    def _clear_pending(self) -> None:
        self.hass.data.get(DOMAIN, {}).pop(_pending_key(self.flow_id), None)

    def _reauth_entry(self) -> config_entries.ConfigEntry:
        entry_id = self.context.get("entry_id")
        if not entry_id:
            raise RuntimeError("reauth flow missing entry_id")
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            raise RuntimeError(f"reauth entry {entry_id} not found")
        return entry

    async def _finalize_entry(
        self,
        cookies_raw: str,
        *,
        title_hint: str | None = None,
    ) -> FlowResult:
        try:
            await self.hass.async_add_executor_job(load_cookies, cookies_raw)
            result = await _validate_connection(self.hass, cookies_raw)
        except Exception as err:
            _LOGGER.exception("Wildberries session validation failed: %s", err)
            raise

        user = result.get("user") or {}
        unique_id = str(
            user.get("user_id") or user_id_from_cookies(cookies_raw) or "wb_account"
        )

        if self._reauth:
            return self.async_update_reload_and_abort(
                self._reauth_entry(),
                data_updates={CONF_COOKIES: cookies_raw},
            )

        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured()
        title_name = user.get("first_name") or title_hint or "Wildberries"
        return self.async_create_entry(
            title=f"Wildberries — {title_name}",
            data={CONF_COOKIES: cookies_raw},
            options={"scan_interval": DEFAULT_SCAN_INTERVAL},
        )

    async def async_step_reauth(self, entry_data: dict[str, Any] | None = None) -> FlowResult:
        self._reauth = True
        return await self.async_step_user()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        menu_options = ["cookies"]
        if _phone_login_supported():
            menu_options.insert(0, "phone")
        return self.async_show_menu(
            step_id="user",
            menu_options=menu_options,
        )

    async def async_step_phone(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            phone = (user_input.get(CONF_PHONE) or "").strip()
            try:
                pending = await self.hass.async_add_executor_job(run_send_code, phone)
                self._save_pending(pending)
                self._phone_confirmation = pending.get("confirmation_type")
                return await self.async_step_phone_code()
            except ImportError:
                errors["base"] = "playwright_missing"
            except RuntimeError as err:
                if "playwright install" in str(err).lower():
                    _LOGGER.error("Chromium install failed: %s", err)
                    errors["base"] = "chromium_install_failed"
                else:
                    _LOGGER.exception("WB phone send-code failed: %s", err)
                    errors["base"] = "phone_login_failed"
            except Exception as err:
                _LOGGER.exception("WB phone send-code failed: %s", err)
                errors["base"] = "phone_login_failed"

        return self.async_show_form(
            step_id="phone",
            data_schema=STEP_PHONE_SCHEMA,
            errors=errors,
        )

    async def async_step_phone_code(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        pending = self._load_pending()
        if not pending:
            return self.async_abort(reason="phone_session_expired")

        if user_input is not None:
            code = (user_input.get(CONF_PHONE_CODE) or "").strip()
            try:
                cookies_raw = await self.hass.async_add_executor_job(
                    run_confirm_code, pending, code
                )
                self._clear_pending()
                return await self._finalize_entry(
                    cookies_raw,
                    title_hint=pending.get("phone"),
                )
            except ImportError:
                errors["base"] = "playwright_missing"
            except Exception as err:
                _LOGGER.exception("WB phone code-confirm failed: %s", err)
                errors["base"] = _map_setup_error(err)

        return self.async_show_form(
            step_id="phone_code",
            data_schema=STEP_PHONE_CODE_SCHEMA,
            errors=errors,
            description_placeholders={
                "confirmation": self._phone_confirmation or "PUSH/SMS",
            },
        )

    async def async_step_cookies(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            cookies_raw = (user_input.get(CONF_COOKIES) or "").strip()
            try:
                return await self._finalize_entry(cookies_raw)
            except Exception as err:
                _LOGGER.exception("Wildberries cookie import failed: %s", err)
                errors["base"] = _map_setup_error(err)

        return self.async_show_form(
            step_id="cookies",
            data_schema=STEP_COOKIES_SCHEMA,
            errors=errors,
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
