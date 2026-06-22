"""Sensor platform for Wildberries Orders."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ACCESS_TOKEN_EXPIRES,
    ATTR_DAYS_REMAINING,
    ATTR_DELIVERY_TYPE,
    ATTR_DETAIL_URL,
    ATTR_ETA,
    ATTR_FETCHED_AT,
    ATTR_LAST_ERROR,
    ATTR_ORDER_DATE,
    ATTR_ORDER_NUMBER,
    ATTR_NM_ID,
    ATTR_ORDER_TITLE,
    ATTR_PAYMENT_STATUS,
    ATTR_PRODUCTS,
    ATTR_PRODUCTS_COUNT,
    ATTR_PRODUCT_TITLES,
    ATTR_REFRESH_TOKEN_EXPIRES,
    ATTR_STORAGE_UNTIL,
    DOMAIN,
)
from .coordinator import WildberriesOrdersCoordinator
from .entity import hub_device_info, order_device_info, sanitize_order_key
from .helpers import get_coordinator, register_platform_add_entities

HUB_SENSORS: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="active_orders",
        translation_key="active_orders",
        icon="mdi:package-variant-closed",
    ),
    SensorEntityDescription(
        key="at_pickup",
        translation_key="at_pickup",
        icon="mdi:store-marker",
    ),
    SensorEntityDescription(
        key="in_transit",
        translation_key="in_transit",
        icon="mdi:truck-delivery",
    ),
    SensorEntityDescription(
        key="session_expires",
        translation_key="session_expires",
        icon="mdi:timer-sand",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class="timestamp",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = get_coordinator(hass, entry.entry_id)
    async_add_entities(WildberriesHubSensor(coordinator, description) for description in HUB_SENSORS)
    register_platform_add_entities(hass, entry, "sensor", async_add_entities)


class WildberriesHubSensor(CoordinatorEntity[WildberriesOrdersCoordinator], SensorEntity):
    """Summary and session sensors on the Wildberries hub device."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator: WildberriesOrdersCoordinator,
        description: SensorEntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.entry.entry_id}_{description.key}"
        self._attr_device_info = hub_device_info(
            coordinator.entry.entry_id,
            (coordinator.data or {}).get("user"),
        )

    @property
    def native_value(self) -> str | int | datetime | None:
        data = self.coordinator.data or {}
        summary = data.get("summary") or {}
        session = data.get("session") or {}
        key = self.entity_description.key

        if key == "active_orders":
            return summary.get("orders_on_page", 0)
        if key == "at_pickup":
            return summary.get("at_pickup_point", 0)
        if key == "in_transit":
            return summary.get("in_transit", 0)
        if key == "session_expires":
            return session.get("session_expires")
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "session_expires":
            if self.entity_description.key == "active_orders":
                return {
                    ATTR_FETCHED_AT: (self.coordinator.data or {}).get("fetched_at"),
                    ATTR_LAST_ERROR: self.coordinator.last_error,
                }
            return None

        session = (self.coordinator.data or {}).get("session") or {}
        attrs: dict[str, Any] = {
            ATTR_DAYS_REMAINING: session.get("days_remaining"),
        }
        if session.get("access_token_expires"):
            attrs[ATTR_ACCESS_TOKEN_EXPIRES] = session["access_token_expires"].isoformat()
        if session.get("refresh_token_expires"):
            attrs[ATTR_REFRESH_TOKEN_EXPIRES] = session["refresh_token_expires"].isoformat()
        return attrs


class WildberriesOrderStatusSensor(CoordinatorEntity[WildberriesOrdersCoordinator], SensorEntity):
    """Status and rich attributes for a single shipment tile."""

    _attr_has_entity_name = True
    _attr_translation_key = "order_status"
    _attr_icon = "mdi:package-variant"

    def __init__(self, coordinator: WildberriesOrdersCoordinator, order_key: str) -> None:
        super().__init__(coordinator)
        self._order_key = order_key
        self._attr_unique_id = f"{coordinator.entry.entry_id}_order_{sanitize_order_key(order_key)}_status"

    @property
    def _order(self) -> dict[str, Any]:
        return (self.coordinator.data or {}).get("orders", {}).get(self._order_key, {})

    @property
    def device_info(self):
        return order_device_info(self.coordinator.entry.entry_id, self._order)

    @property
    def native_value(self) -> str | None:
        return self._order.get("status")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        order = self._order
        products = order.get("products") or []
        product_titles = [item.get("title") for item in products if item.get("title")]
        return {
            ATTR_ORDER_NUMBER: order.get("order_number"),
            ATTR_NM_ID: order.get("nm_id"),
            ATTR_ORDER_DATE: order.get("order_date"),
            ATTR_ORDER_TITLE: order.get("order_title"),
            ATTR_ETA: order.get("eta_text"),
            ATTR_DELIVERY_TYPE: order.get("delivery_type"),
            ATTR_STORAGE_UNTIL: order.get("storage_until"),
            ATTR_PRODUCTS_COUNT: order.get("products_count"),
            ATTR_PRODUCT_TITLES: product_titles,
            ATTR_PRODUCTS: products,
            ATTR_PAYMENT_STATUS: order.get("payment_status"),
            ATTR_DETAIL_URL: order.get("detail_url"),
            "is_at_pickup_point": order.get("is_at_pickup_point"),
            "is_in_transit": order.get("is_in_transit"),
            "timeline_steps": order.get("timeline_steps"),
        }
