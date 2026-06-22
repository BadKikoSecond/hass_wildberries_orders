#!/usr/bin/env python3
"""Probe Wildberries API access from current Python environment (e.g. HA container)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

API_URL = "https://www.wildberries.ru/webapi/lk/myorders/delivery/active"
WARMUP_URL = "https://www.wildberries.ru/lk/myorders/delivery"
HEADERS = {
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


def load_cookies(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    jar: dict[str, str] = {}
    items = data if isinstance(data, list) else data.get("cookies", [])
    for item in items:
        if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
            jar[str(item["name"])] = str(item["value"])
    return jar


async def test_aiohttp(cookies: dict[str, str]) -> None:
    import aiohttp
    from yarl import URL

    print("=== aiohttp ===")
    jar = aiohttp.CookieJar(unsafe=True)
    jar.update_cookies(cookies, response_url=URL("https://www.wildberries.ru"))
    async with aiohttp.ClientSession(headers=HEADERS, cookie_jar=jar) as session:
        try:
            async with session.get(WARMUP_URL, allow_redirects=True, max_redirects=5) as resp:
                print("warmup", resp.status, (resp.headers.get("content-type") or "")[:40])
        except Exception as err:
            print("warmup_error", type(err).__name__, err)
        try:
            async with session.post(API_URL, allow_redirects=True, max_redirects=5) as resp:
                body = await resp.text()
                print("api", resp.status, (resp.headers.get("content-type") or "")[:40])
                print("body", body[:180].replace("\n", " "))
        except Exception as err:
            print("api_error", type(err).__name__, err)


async def test_curl_cffi(cookies: dict[str, str], impersonate: str) -> None:
    from curl_cffi.requests import AsyncSession

    print(f"=== curl_cffi {impersonate} ===")
    try:
        async with AsyncSession(impersonate=impersonate) as session:
            r = await session.get(WARMUP_URL, cookies=cookies, headers=HEADERS)
            print("warmup", r.status_code, (r.headers.get("content-type") or "")[:40])
            r2 = await session.post(API_URL, cookies=cookies, headers=HEADERS)
            print("api", r2.status_code, (r2.headers.get("content-type") or "")[:40])
            if r2.status_code == 200 and "json" in (r2.headers.get("content-type") or ""):
                value = (r2.json().get("value") or {})
                positions = value.get("positions") or []
                print("positions", len(positions))
            else:
                print("body", r2.text[:180].replace("\n", " "))
    except Exception as err:
        print("error", type(err).__name__, err)


async def main() -> None:
    cookies = load_cookies(COOKIES_PATH)
    print("cookies", len(cookies), "names sample", list(cookies)[:4])
    await test_aiohttp(cookies)
    try:
        import curl_cffi  # noqa: F401
    except ImportError as err:
        print("=== curl_cffi ===", err)
        return
    for profile in ("chrome120", "chrome131", "chrome124", "safari17_0"):
        await test_curl_cffi(cookies, profile)


COOKIES_PATH = Path(sys.argv[1] if len(sys.argv) > 1 else "cookies.json")

if __name__ == "__main__":
    asyncio.run(main())
