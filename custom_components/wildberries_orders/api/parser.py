"""Parse Wildberries buyer delivery API responses into HA-friendly structures."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_AT_PICKUP_RE = re.compile(
    r"(готов к получению|можно забирать|ожидает в пункте|в пункте выдачи|"
    r"прибыл в пункт|готов к выдаче|хранится до|ожидает получения)",
    re.I,
)
_IN_TRANSIT_RE = re.compile(
    r"(в пути|переда[её]тся|переда[её]м|доставля|на сборке|собирается|"
    r"оформлен|передан в доставку|едет|в службе доставки)",
    re.I,
)
_STORAGE_UNTIL_RE = re.compile(r"хранится до\s+(.+)", re.I)


def parse_active_deliveries(payload: dict[str, Any]) -> dict[str, Any]:
    """Parse ``/webapi/v2/lk/myorders/delivery/active`` value object."""
    positions = payload.get("positions") or []
    orders = [_parse_position(item, index) for index, item in enumerate(positions)]
    return {
        "user": _user_info(payload),
        "orders": orders,
        "qr_code": payload.get("qrCode"),
        "summary": _build_summary(orders),
    }


def _parse_position(item: dict[str, Any], index: int) -> dict[str, Any]:
    nm_id = item.get("code1S") or item.get("nmId") or item.get("nm_id")
    rid = item.get("rId") or item.get("rid") or item.get("orderId")
    status = _normalize_text(item.get("trackingStatus") or item.get("status") or "")
    eta_text = _normalize_text(
        item.get("deliveryDate")
        or item.get("expectedDelivery")
        or item.get("dateTime")
        or item.get("statusDate")
        or ""
    )
    order_date = _parse_order_date(item.get("orderDate") or item.get("createdAt"))
    delivery_type = _normalize_text(item.get("officeName") or item.get("address") or "")
    payment_status = _normalize_text(item.get("paymentType") or item.get("payState") or "")
    price = item.get("price") or item.get("priceWithDiscount")
    status_blob = " ".join(filter(None, [status, eta_text, delivery_type]))

    product = {
        "title": _normalize_text(item.get("name") or item.get("goodsName") or ""),
        "price_text": _format_price(price),
        "image_url": product_image_url(nm_id),
        "product_url": product_url(nm_id),
    }

    order_key = f"{rid or nm_id or 'item'}_{index}"

    return {
        "order_key": order_key,
        "order_number": str(rid) if rid is not None else str(nm_id or order_key),
        "nm_id": nm_id,
        "status": status or "В доставке",
        "delivery_type": delivery_type,
        "eta_text": eta_text or None,
        "order_date": order_date,
        "order_title": f"Заказ {order_date}" if order_date else None,
        "is_at_pickup_point": bool(_AT_PICKUP_RE.search(status_blob)),
        "is_in_transit": bool(_IN_TRANSIT_RE.search(status_blob)),
        "storage_until": _storage_until(status_blob),
        "products_count": 1,
        "tile_products": [product] if nm_id else [],
        "products": [product] if product.get("title") or nm_id else [],
        "payment_status": payment_status or None,
        "detail_url": "https://www.wildberries.ru/lk/myorders/delivery",
        "device_name": None,
    }


def _user_info(payload: dict[str, Any]) -> dict[str, Any]:
    profile = payload.get("profile") or payload.get("user") or {}
    return {
        "user_id": profile.get("id") or profile.get("userId"),
        "is_logged_in": True,
        "phone": profile.get("phone"),
        "first_name": profile.get("firstName") or profile.get("name"),
    }


def _build_summary(orders: list[dict[str, Any]]) -> dict[str, int]:
    at_pickup = sum(1 for order in orders if order.get("is_at_pickup_point"))
    in_transit = sum(1 for order in orders if order.get("is_in_transit"))
    return {
        "orders_on_page": len(orders),
        "at_pickup_point": at_pickup,
        "in_transit": in_transit,
        "tracking_items": len(orders),
    }


def parse_user_grade(payload: dict[str, Any]) -> dict[str, int]:
    """Parse user-grade payload (completed purchase counters)."""
    result: dict[str, int] = {}
    order_count = payload.get("order_count")
    period_count = payload.get("period_order_count")
    if isinstance(order_count, int):
        result["past_purchases_count"] = order_count
    if isinstance(period_count, int):
        result["period_purchases_count"] = period_count
    return result


def _normalize_text(text: str | None) -> str:
    if not text:
        return ""
    return str(text).replace("\u00a0", " ").replace("\u202f", " ").strip()


def _parse_order_date(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if "T" in text:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            pass
    return text


def _format_price(value: Any) -> str | None:
    if value is None:
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return str(value)
    if amount > 1000:
        amount /= 100
    return f"{amount:,.0f} ₽".replace(",", " ")


def _storage_until(text: str | None) -> str | None:
    if not text:
        return None
    match = _STORAGE_UNTIL_RE.search(text)
    return match.group(1).strip() if match else None


def product_url(nm_id: Any) -> str | None:
    if not nm_id:
        return None
    return f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"


def product_image_url(nm_id: Any) -> str | None:
    if not nm_id:
        return None
    try:
        article = int(nm_id)
    except (TypeError, ValueError):
        return None
    vol = article // 100000
    part = article // 1000
    host = _image_host(vol)
    return f"https://basket-{host}.wbbasket.ru/vol{vol}/part{part}/{article}/images/c246x328/1.jpg"


def _image_host(vol: int) -> str:
    hosts = (
        (143, "01"),
        (287, "02"),
        (431, "03"),
        (719, "04"),
        (1007, "05"),
        (1061, "06"),
        (1115, "07"),
        (1169, "08"),
        (1313, "09"),
        (1601, "10"),
        (1655, "11"),
        (1919, "12"),
        (2045, "13"),
        (2189, "14"),
        (2405, "15"),
        (2621, "16"),
        (2837, "17"),
        (3053, "18"),
        (3269, "19"),
        (3485, "20"),
    )
    for limit, host in hosts:
        if vol <= limit:
            return host
    return "21"
