"""Load cookies from JSON (browser export, dict, or Playwright storage_state)."""

from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

CookieJar = dict[str, str]

_AUTH_COOKIE_NAMES = (
    "wbx-validation-key",
    "WBToken",
    "WBTokenV3",
    "_wbSes",
)

_BEARER_COOKIE_NAMES = ("WBTokenV3", "WBToken")

_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def parse_cookies_input(text: str) -> Any:
    """Parse cookies pasted from browser extensions or DevTools."""
    cleaned = _CODE_FENCE_RE.sub("", text.strip()).strip()
    if not cleaned:
        raise ValueError("empty cookies input")

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    if "=" in cleaned and not cleaned.startswith(("[", "{")):
        jar: CookieJar = {}
        for part in cleaned.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            name, _, value = part.partition("=")
            name = name.strip()
            value = value.strip()
            if name:
                jar[name] = value
        if jar:
            return jar

    raise ValueError("unsupported cookies format")


def load_cookies(source: str | Path | Mapping[str, Any] | list[Any]) -> CookieJar:
    """Normalize cookies to ``{name: value}`` for the ``Cookie`` header."""
    if isinstance(source, str) and not Path(source).exists():
        source = parse_cookies_input(source)

    data = _read_source(source)
    jar: CookieJar = {}

    if isinstance(data, dict) and "cookies" in data and isinstance(data["cookies"], list):
        _merge_cookie_list(jar, data["cookies"])
        _merge_local_storage(jar, data)
    elif isinstance(data, list):
        _merge_cookie_list(jar, data)
    elif isinstance(data, dict):
        for key, value in data.items():
            if key in ("cookies", "origins", "localStorage"):
                continue
            if isinstance(value, str):
                jar[key] = value
        _merge_local_storage(jar, data)
    else:
        raise ValueError("Unsupported cookies JSON format")

    if not jar:
        raise ValueError("No cookies found in JSON")

    if not bearer_token_from_jar(jar):
        raise ValueError(
            "Missing WBTokenV3 (OAuth access token). "
            "Добавьте в JSON cookie с именем WBTokenV3 — значение из "
            "DevTools → Application → localStorage → wbx__tokenData → token. "
            "Либо экспортируйте Playwright storage_state с origins/localStorage."
        )

    token = bearer_token_from_jar(jar)
    if token and not is_buyer_api_token(token):
        raise ValueError(
            "WBTokenV3 is wb-id login token, not buyer API token. "
            "Повторите вход по телефону в интеграции."
        )

    if not jar.get("x_wbaas_token"):
        raise ValueError(
            "Missing x_wbaas_token (antibot cookie). "
            "Экспортируйте все cookies домена .wildberries.ru из Firefox."
        )

    return jar


def bearer_token_from_jar(jar: CookieJar) -> str | None:
    """OAuth access token stored as WBTokenV3/WBToken in the export."""
    for name in _BEARER_COOKIE_NAMES:
        token = jar.get(name)
        if token:
            return token
    return None


def is_buyer_api_token(token: str) -> bool:
    """True when JWT is for wildberries.ru buyer API (not wb-id login token)."""
    payload = _decode_jwt_payload(token)
    if not payload:
        return False
    client_id = payload.get("client_id")
    return client_id not in (None, "wb-id")


def request_cookies(jar: CookieJar) -> CookieJar:
    """Cookies for HTTP requests (JWT is sent via Authorization, not Cookie)."""
    return {name: value for name, value in jar.items() if name not in _BEARER_COOKIE_NAMES}


def auth_headers(jar: CookieJar) -> dict[str, str]:
    token = bearer_token_from_jar(jar)
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def cookies_header(jar: CookieJar) -> str:
    return "; ".join(f"{name}={value}" for name, value in jar.items())


def session_expiry_info(source: str | Path | Mapping[str, Any] | list[Any]) -> dict[str, Any]:
    """Return auth cookie expiry timestamps parsed from the raw export."""
    if isinstance(source, str) and not Path(source).exists():
        source = parse_cookies_input(source)

    data = _read_source(source)
    items = _cookie_items(data)
    expiries: dict[str, datetime] = {}
    for item in items:
        name = item.get("name")
        if name not in _AUTH_COOKIE_NAMES:
            continue
        exp = _cookie_expiry_datetime(item)
        if exp is not None:
            expiries[str(name)] = exp

    jar: CookieJar = {}
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        _merge_cookie_list(jar, data["cookies"])
    elif isinstance(data, list):
        _merge_cookie_list(jar, data)
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str) and key not in ("cookies", "origins", "localStorage"):
                jar[key] = value
    _merge_local_storage(jar, data if isinstance(data, dict) else {})

    token_exp = jwt_expiry_from_jar(jar)
    if token_exp is not None:
        expiries["WBTokenV3"] = token_exp

    if not expiries:
        return {
            "access_token_expires": None,
            "refresh_token_expires": None,
            "session_expires": None,
            "days_remaining": None,
        }

    validation = expiries.get("wbx-validation-key")
    wb_token = expiries.get("WBToken") or expiries.get("WBTokenV3")
    session = min(expiries.values())
    days = None
    if session:
        days = max(0, (session - datetime.now(timezone.utc)).total_seconds() / 86400)

    return {
        "access_token_expires": validation or wb_token,
        "refresh_token_expires": wb_token,
        "session_expires": session,
        "days_remaining": round(days, 1) if days is not None else None,
    }


def user_id_from_cookies(source: str | Path | Mapping[str, Any] | list[Any]) -> str | None:
    """Best-effort stable account id from exported cookies."""
    if isinstance(source, str) and not Path(source).exists():
        source = parse_cookies_input(source)
    data = _read_source(source)
    jar: CookieJar = {}
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        _merge_cookie_list(jar, data["cookies"])
    elif isinstance(data, list):
        _merge_cookie_list(jar, data)
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str) and key not in ("cookies", "origins", "localStorage"):
                jar[key] = value
    for name in ("_wbauid", "___wbu", "BasketUID"):
        if jar.get(name):
            return jar[name]
    token = bearer_token_from_jar(jar)
    if token:
        sub = jwt_subject(token)
        if sub:
            return sub
    return None


def jwt_expiry_from_jar(jar: CookieJar) -> datetime | None:
    token = bearer_token_from_jar(jar)
    if not token:
        return None
    return jwt_expiry(token)


def jwt_expiry(token: str) -> datetime | None:
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        return None
    return datetime.fromtimestamp(exp, tz=timezone.utc)


def jwt_subject(token: str) -> str | None:
    payload = _decode_jwt_payload(token)
    if not payload:
        return None
    sub = payload.get("sub") or payload.get("user")
    return str(sub) if sub is not None else None


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError, json.JSONDecodeError, TypeError):
        return None
    return payload if isinstance(payload, dict) else None


def _merge_local_storage(jar: CookieJar, data: Mapping[str, Any]) -> None:
    """Pull WBTokenV3 from Playwright storage_state or a localStorage block."""
    if jar.get("WBTokenV3") or jar.get("WBToken"):
        return

    token = _token_from_local_storage_value(data.get("localStorage"))
    if token:
        jar["WBTokenV3"] = token
        return

    oauth = data.get("wbid-oauth-sdk-access-token")
    if isinstance(oauth, str) and oauth.count(".") >= 2:
        jar["WBTokenV3"] = oauth
        return

    for origin in data.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        for item in origin.get("localStorage") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if name == "wbx__tokenData":
                token = _token_from_local_storage_value(item.get("value"))
                if token:
                    jar["WBTokenV3"] = token
                    return
            if name == "wbid-oauth-sdk-access-token":
                value = item.get("value")
                if isinstance(value, str) and value.count(".") >= 2:
                    jar["WBTokenV3"] = value
                    return


def _token_from_local_storage_value(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, dict):
        token = value.get("token")
        return str(token) if token else None
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        if isinstance(parsed, dict) and parsed.get("token"):
            return str(parsed["token"])
    return None


def _cookie_items(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, dict) and isinstance(data.get("cookies"), list):
        return [c for c in data["cookies"] if isinstance(c, dict)]
    if isinstance(data, list):
        return [c for c in data if isinstance(c, dict)]
    return []


def _cookie_expiry_datetime(item: dict[str, Any]) -> datetime | None:
    exp = item.get("expirationDate") or item.get("expires")
    if not exp:
        return None
    if isinstance(exp, (int, float)):
        return datetime.fromtimestamp(exp, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
    except ValueError:
        return None


def _read_source(source: str | Path | Mapping[str, Any] | list[Any]) -> Any:
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_file():
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        return parse_cookies_input(str(source))
    return source


def _merge_cookie_list(jar: CookieJar, items: list[Any]) -> None:
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if not name or value is None:
            continue
        jar[str(name)] = str(value)
