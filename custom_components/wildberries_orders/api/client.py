"""Async HTTP client for Wildberries buyer delivery pages."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from curl_cffi.requests import AsyncSession

from .cookies import CookieJar, user_id_from_cookies
from .enrich import enrich_orders
from .errors import WildberriesAntibotError, WildberriesAuthError
from .parser import parse_active_deliveries

_LOGGER = logging.getLogger(__name__)

BASE_URL = "https://www.wildberries.ru"
ACTIVE_DELIVERIES_URL = f"{BASE_URL}/webapi/lk/myorders/delivery/active"
CARD_API_URL = "https://card.wb.ru/cards/v2/detail"
BROWSER_IMPERSONATE = "chrome120"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.wildberries.ru/lk/myorders/delivery",
    "Origin": "https://www.wildberries.ru",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Linux"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
}


class WildberriesOrdersClient:
    """Fetch buyer deliveries via Wildberries webapi using exported cookies."""

    def __init__(
        self,
        cookies: CookieJar,
        *,
        session: AsyncSession | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._cookies = cookies
        self._session = session
        self._owns_session = session is None
        self._timeout = timeout
        self._warmed_up = False

    async def __aenter__(self) -> WildberriesOrdersClient:
        if self._session is None:
            self._session = AsyncSession(
                impersonate=BROWSER_IMPERSONATE,
                timeout=self._timeout,
            )
        return self

    async def __aexit__(self, *args: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()

    async def _warmup(self) -> None:
        if self._warmed_up or self._session is None:
            return
        try:
            await self._session.get(
                f"{BASE_URL}/lk/myorders/delivery",
                cookies=self._cookies,
                headers=DEFAULT_HEADERS,
            )
        except Exception as err:
            _LOGGER.debug("Wildberries warmup request failed: %s", err)
        self._warmed_up = True

    async def _post_json(self, url: str) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Use async with WildberriesOrdersClient(...)")

        await self._warmup()
        response = await self._session.post(
            url,
            cookies=self._cookies,
            headers=DEFAULT_HEADERS,
        )
        body = response.text
        if response.status_code in (401, 403):
            raise _map_access_error(response.status_code, body)
        if response.status_code == 498:
            raise WildberriesAntibotError(
                "HTTP 498: Wildberries antibot. Обновите cookies из браузера, "
                "где вы уже прошли проверку."
            )
        if response.status_code != 200:
            raise WildberriesAntibotError(f"HTTP {response.status_code}: {body[:300]}")

        content_type = response.headers.get("Content-Type", "")
        if "json" not in content_type.lower():
            raise WildberriesAntibotError("Non-JSON response (likely antibot HTML)")

        data = response.json()
        if not isinstance(data, dict):
            raise WildberriesAuthError("Unexpected Wildberries response format")

        if data.get("error") or data.get("isSuccess") is False:
            message = data.get("errorText") or data.get("errorMsg") or "session rejected"
            raise WildberriesAuthError(f"Wildberries API error: {message}")

        return data

    async def fetch_active_deliveries(self) -> dict[str, Any]:
        data = await self._post_json(ACTIVE_DELIVERIES_URL)
        value = data.get("value")
        if value is None:
            raise WildberriesAuthError(
                "Wildberries не вернул активные доставки. "
                "Экспортируйте cookies с wildberries.ru из браузера, где вы залогинены."
            )
        if not isinstance(value, dict):
            raise WildberriesAuthError("Wildberries session rejected (empty value)")

        parsed = parse_active_deliveries(value)
        user = parsed.get("user") or {}
        if not user.get("user_id"):
            user["user_id"] = user_id_from_cookies(self._cookies)
        parsed["user"] = user

        orders = parsed.get("orders") or []
        cards = await self._fetch_cards([order.get("nm_id") for order in orders])
        enrich_orders(orders, cards)
        for order in orders:
            if not order.get("device_name"):
                from .enrich import _device_name

                order["device_name"] = _device_name(order)

        return {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": ACTIVE_DELIVERIES_URL,
            **parsed,
        }

    async def fetch_order_list(self, *, active_only: bool = True) -> dict[str, Any]:
        if not active_only:
            _LOGGER.debug("Wildberries supports active deliveries only in this integration")
        return await self.fetch_active_deliveries()

    async def _fetch_cards(self, nm_ids: list[Any]) -> dict[int, dict[str, Any]]:
        if self._session is None:
            return {}

        unique: list[int] = []
        for nm_id in nm_ids:
            if nm_id is None:
                continue
            try:
                article = int(nm_id)
            except (TypeError, ValueError):
                continue
            if article not in unique:
                unique.append(article)

        cards: dict[int, dict[str, Any]] = {}
        for article in unique[:20]:
            try:
                response = await self._session.get(
                    CARD_API_URL,
                    params={
                        "appType": 1,
                        "curr": "rub",
                        "dest": -1257786,
                        "spp": 30,
                        "nm": article,
                    },
                    cookies=self._cookies,
                    headers={
                        **DEFAULT_HEADERS,
                        "Referer": f"https://www.wildberries.ru/catalog/{article}/detail.aspx",
                        "Origin": "https://www.wildberries.ru",
                    },
                )
            except Exception as err:
                _LOGGER.debug("Card API request failed for %s: %s", article, err)
                continue
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            products = (payload.get("data") or {}).get("products") or []
            if products and isinstance(products[0], dict):
                cards[article] = products[0]
        return cards


def _map_access_error(status: int, body: str) -> WildberriesAntibotError | WildberriesAuthError:
    snippet = body[:500].replace("\n", " ")
    _LOGGER.error("Wildberries HTTP %s body: %s", status, snippet)

    lowered = body.lower()
    if status == 403 or any(
        marker in lowered for marker in ("antibot", "wbaas", "<html", "captcha", "access denied")
    ):
        return WildberriesAntibotError(
            "HTTP 403: Wildberries antibot. Обновите cookies из браузера, "
            "где вы уже прошли проверку."
        )
    return WildberriesAuthError(f"HTTP {status}: сессия отклонена — {snippet[:200]}")
