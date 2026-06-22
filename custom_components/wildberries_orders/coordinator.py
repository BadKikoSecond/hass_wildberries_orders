"""DataUpdateCoordinator for Wildberries buyer deliveries."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.client import WildberriesOrdersClient
from .api.cookies import load_cookies, session_expiry_info
from .api.errors import WildberriesAntibotError, WildberriesAuthError, WildberriesOrdersError
from .const import CONF_COOKIES, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class WildberriesOrdersCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch Wildberries active deliveries and session metadata."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        scan_minutes = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=scan_minutes),
        )
        self.entry = entry
        self.connection_ok = True
        self.last_error: str | None = None

    @property
    def cookies_raw(self) -> str:
        return self.entry.data[CONF_COOKIES]

    async def _async_update_data(self) -> dict[str, Any]:
        cookies_raw = self.cookies_raw
        try:
            cookies = await self.hass.async_add_executor_job(load_cookies, cookies_raw)
            session = await self.hass.async_add_executor_job(session_expiry_info, cookies_raw)
            async with WildberriesOrdersClient(cookies) as client:
                payload = await client.fetch_active_deliveries()
        except (WildberriesAuthError, WildberriesAntibotError, WildberriesOrdersError, OSError, ValueError) as err:
            self.connection_ok = False
            self.last_error = str(err)
            _LOGGER.error("Wildberries update failed: %s", err)
            raise UpdateFailed(str(err)) from err

        self.connection_ok = True
        self.last_error = None

        orders = {
            order["order_key"]: order
            for order in payload.get("orders") or []
            if order.get("order_key")
        }

        return {
            "orders": orders,
            "summary": payload.get("summary") or {},
            "tracking": payload.get("orders") or [],
            "user": payload.get("user") or {},
            "session": session,
            "fetched_at": payload.get("fetched_at"),
            "qr_code": payload.get("qr_code"),
        }
