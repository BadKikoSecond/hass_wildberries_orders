"""Manage dynamic per-order entities."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .binary_sensor import WildberriesOrderBinarySensor
from .coordinator import WildberriesOrdersCoordinator
from .sensor import WildberriesOrderStatusSensor


class WildberriesOrderEntityManager:
    """Add order sensors and binary sensors when the order list changes."""

    def __init__(
        self,
        coordinator: WildberriesOrdersCoordinator,
        sensor_add_entities: AddEntitiesCallback,
        binary_add_entities: AddEntitiesCallback,
    ) -> None:
        self.coordinator = coordinator
        self._sensor_add_entities = sensor_add_entities
        self._binary_add_entities = binary_add_entities
        self._known_keys: set[str] = set()

    def async_setup(self) -> None:
        self._async_add_order_entities()
        self.coordinator.async_add_listener(self._handle_coordinator_update)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._async_add_order_entities()

    @callback
    def _async_add_order_entities(self) -> None:
        if not self.coordinator.data:
            return

        orders = self.coordinator.data.get("orders") or {}
        new_keys = set(orders.keys()) - self._known_keys
        if not new_keys:
            return

        sensors = []
        binaries = []
        for key in sorted(new_keys):
            sensors.append(WildberriesOrderStatusSensor(self.coordinator, key))
            binaries.append(WildberriesOrderBinarySensor(self.coordinator, key, "at_pickup"))
            binaries.append(WildberriesOrderBinarySensor(self.coordinator, key, "in_transit"))

        self._known_keys.update(new_keys)
        self._sensor_add_entities(sensors)
        self._binary_add_entities(binaries)
