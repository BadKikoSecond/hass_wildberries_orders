"""Shared setup helpers (kept separate to avoid circular imports)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import WildberriesOrdersCoordinator


def get_coordinator(hass: HomeAssistant, entry_id: str) -> WildberriesOrdersCoordinator:
    return hass.data[DOMAIN][entry_id]["coordinator"]


def register_platform_add_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    platform: str,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Store platform callbacks and start the order entity manager when both are ready."""
    from .entity_manager import WildberriesOrderEntityManager

    entry_data = hass.data[DOMAIN][entry.entry_id]
    key = f"{platform}_add_entities"
    entry_data[key] = async_add_entities

    if entry_data["order_manager"] is not None:
        return
    if not entry_data["sensor_add_entities"] or not entry_data["binary_add_entities"]:
        return

    manager = WildberriesOrderEntityManager(
        entry_data["coordinator"],
        entry_data["sensor_add_entities"],
        entry_data["binary_add_entities"],
    )
    manager.async_setup()
    entry_data["order_manager"] = manager
