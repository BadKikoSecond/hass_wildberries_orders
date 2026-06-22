"""Shared helpers for Wildberries Orders entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN, MANUFACTURER


def sanitize_order_key(order_key: str) -> str:
    return order_key.replace("-", "_").replace(" ", "_")


def hub_device_info(entry_id: str, user: dict | None = None) -> DeviceInfo:
    name = "Wildberries"
    if user and user.get("first_name"):
        name = f"Wildberries — {user['first_name']}"
    elif user and user.get("phone"):
        name = f"Wildberries — {user['phone']}"
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=name,
        manufacturer=MANUFACTURER,
        model="Аккаунт покупателя",
    )


def order_device_info(entry_id: str, order: dict) -> DeviceInfo:
    order_number = order.get("order_number") or "unknown"
    status = order.get("status") or ""
    products = order.get("products") or []
    product_titles = [item.get("title") for item in products if item.get("title")]

    name = order.get("device_name") or f"Заказ {order_number}"
    model = _order_model(status, product_titles, order.get("products_count"))

    return DeviceInfo(
        identifiers={(DOMAIN, entry_id, order.get("order_key", order_number))},
        name=name,
        manufacturer=MANUFACTURER,
        model=model,
        via_device=(DOMAIN, entry_id),
    )


def _order_model(status: str, product_titles: list[str], products_count: int | None) -> str:
    count = products_count or len(product_titles)
    if product_titles:
        if len(product_titles) == 1:
            return f"{status} • {product_titles[0][:48]}"
        return f"{status} • {len(product_titles)} тов."
    if count:
        return f"{status} • {count} тов."
    return status[:60] if status else "Доставка"
