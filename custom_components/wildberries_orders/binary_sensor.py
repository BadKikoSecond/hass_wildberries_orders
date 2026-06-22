"""Binary sensor platform for Wildberries Orders."""

from __future__ import annotations

from typing import Any, Literal

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import WildberriesOrdersCoordinator
from .entity import hub_device_info, order_device_info, sanitize_order_key
from .helpers import get_coordinator, register_platform_add_entities

BinaryKind = Literal["at_pickup", "in_transit"]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = get_coordinator(hass, entry.entry_id)
    async_add_entities([WildberriesSessionBinarySensor(coordinator)])
    register_platform_add_entities(hass, entry, "binary", async_add_entities)


class WildberriesSessionBinarySensor(CoordinatorEntity[WildberriesOrdersCoordinator], BinarySensorEntity):
    """Whether Wildberries cookies/session still work."""

    _attr_translation_key = "session_valid"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_icon = "mdi:cloud-check"

    def __init__(self, coordinator: WildberriesOrdersCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry.entry_id}_session_valid"
        self._attr_device_info = hub_device_info(
            coordinator.entry.entry_id,
            (coordinator.data or {}).get("user"),
        )

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.connection_ok


class WildberriesOrderBinarySensor(CoordinatorEntity[WildberriesOrdersCoordinator], BinarySensorEntity):
    """Per-shipment pickup / in-transit flags for automations."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: WildberriesOrdersCoordinator,
        order_key: str,
        kind: BinaryKind,
    ) -> None:
        super().__init__(coordinator)
        self._order_key = order_key
        self._kind = kind
        self._attr_unique_id = (
            f"{coordinator.entry.entry_id}_order_{sanitize_order_key(order_key)}_{kind}"
        )

        if kind == "at_pickup":
            self._attr_translation_key = "order_at_pickup"
            self._attr_icon = "mdi:store-check"
        else:
            self._attr_translation_key = "order_in_transit"
            self._attr_icon = "mdi:truck-fast"

    def _order(self, order_key: str | None = None) -> dict[str, Any]:
        key = order_key or self._order_key
        return (self.coordinator.data or {}).get("orders", {}).get(key, {})

    @property
    def device_info(self):
        return order_device_info(self.coordinator.entry.entry_id, self._order())

    @property
    def is_on(self) -> bool | None:
        order = self._order()
        if not order:
            return None
        if self._kind == "at_pickup":
            return bool(order.get("is_at_pickup_point"))
        return bool(order.get("is_in_transit"))
