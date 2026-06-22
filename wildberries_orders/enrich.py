"""Enrich delivery positions with product titles from the public card API."""

from __future__ import annotations

from typing import Any

from .parser import product_image_url, product_url


def enrich_orders(orders: list[dict[str, Any]], cards: dict[int, dict[str, Any]]) -> None:
    """Merge card API data into parsed delivery positions."""
    for order in orders:
        nm_id = order.get("nm_id")
        if nm_id is None:
            continue
        try:
            card = cards.get(int(nm_id))
        except (TypeError, ValueError):
            card = None
        if not card:
            _ensure_product_stub(order)
            continue

        title = card.get("name") or card.get("title")
        brand = card.get("brand")
        full_title = " ".join(filter(None, [brand, title])).strip() or title
        price = card.get("salePriceU") or card.get("priceU")
        product = {
            "title": full_title,
            "price_text": order.get("products", [{}])[0].get("price_text") if order.get("products") else None,
            "image_url": product_image_url(nm_id),
            "product_url": product_url(nm_id),
        }
        if price is not None:
            try:
                product["price_text"] = f"{float(price) / 100:,.0f} ₽".replace(",", " ")
            except (TypeError, ValueError):
                pass

        order["products"] = [product]
        order["tile_products"] = [product]
        order["products_count"] = 1
        if not order.get("device_name"):
            order["device_name"] = _device_name(order)


def _ensure_product_stub(order: dict[str, Any]) -> None:
    nm_id = order.get("nm_id")
    if not nm_id:
        return
    product = {
        "title": f"Товар {nm_id}",
        "price_text": None,
        "image_url": product_image_url(nm_id),
        "product_url": product_url(nm_id),
    }
    order["products"] = [product]
    order["tile_products"] = [product]
    order["products_count"] = 1
    if not order.get("device_name"):
        order["device_name"] = _device_name(order)


def _device_name(order: dict[str, Any]) -> str:
    order_date = order.get("order_date")
    status = (order.get("status") or "").strip()
    order_number = order.get("order_number") or "?"
    products = order.get("products") or []
    if products and products[0].get("title"):
        title = str(products[0]["title"])
        if len(title) > 48:
            title = title[:45] + "..."
        if status:
            return f"{title} — {status}"
        return title
    if order_date and status:
        return f"{order_date} — {status}"
    if order_date:
        return order_date
    return f"Заказ {order_number}"
