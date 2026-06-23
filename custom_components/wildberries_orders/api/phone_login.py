"""WB ID phone login via headless Playwright (antibot bypass: non-headless UA)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlunparse

try:
    from .cookies import _decode_jwt_payload, is_buyer_api_token
except ImportError:
    import importlib.util as _importlib_util
    from pathlib import Path as _Path

    _cookies_path = _Path(__file__).resolve().parent / "cookies.py"
    _spec = _importlib_util.spec_from_file_location(
        "wildberries_orders_api_cookies", _cookies_path
    )
    _cookies_mod = _importlib_util.module_from_spec(_spec)
    assert _spec and _spec.loader
    _spec.loader.exec_module(_cookies_mod)
    is_buyer_api_token = _cookies_mod.is_buyer_api_token
    _decode_jwt_payload = _cookies_mod._decode_jwt_payload

_LOGGER = logging.getLogger(__name__)

ID_BASE = "https://id.wb.ru"
WB_BASE = "https://www.wildberries.ru"

# Playwright headless defaults to HeadlessChrome UA; WB antibot rejects that fingerprint.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}) };
"""

# Docker / HA container: --no-sandbox; low RAM: --disable-dev-shm-usage
CHROMIUM_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
]

_chromium_ready = False


async def ensure_chromium() -> None:
    """Download Chromium on first login if Playwright has no browser yet."""
    global _chromium_ready
    if _chromium_ready:
        return
    try:
        from playwright.async_api import async_playwright
    except ImportError as err:
        raise ImportError("playwright not installed") from err

    try:
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=CHROMIUM_LAUNCH_ARGS)
        await browser.close()
        await pw.stop()
        _chromium_ready = True
        return
    except Exception as err:
        _LOGGER.info("Chromium missing, running playwright install: %s", err)

    loop = asyncio.get_running_loop()
    proc = await loop.run_in_executor(
        None,
        lambda: subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            check=False,
        ),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "playwright install chromium failed: "
            f"{(proc.stderr or proc.stdout or '').strip()}"
        )
    _chromium_ready = True


@dataclass
class SendCodeResult:
    confirmation_type: str
    sticker: str
    flow_id: str
    nonce: str
    deny_resend_until: str | None = None


@dataclass
class PendingPhoneLogin:
    """Serializable state between HA config-flow steps."""

    phone: str
    browser_state: dict[str, Any]
    flow: dict[str, Any]
    send: dict[str, Any]
    nonce: str
    pkce_verifier: str = ""
    oauth_state: str = ""


@dataclass
class LoginSessionExport:
    cookies: list[dict[str, Any]]
    local_storage: dict[str, str]
    storage_state: dict[str, Any] | None = None

    def to_cookies_json(self) -> str:
        if self.storage_state:
            return json.dumps(self.storage_state, ensure_ascii=False)

        items = [
            {
                "name": c["name"],
                "value": c["value"],
                "domain": c.get("domain") or ".wildberries.ru",
                "path": c.get("path", "/"),
                "secure": c.get("secure", True),
                "httpOnly": c.get("httpOnly", False),
                "sameSite": c.get("sameSite", "lax"),
            }
            for c in self.cookies
            if "wildberries.ru" in (c.get("domain") or "") or c.get("name") == "x_wbaas_token"
        ]
        access = _buyer_token_from_storage(self.local_storage)
        if access:
            items.append(
                {
                    "name": "WBTokenV3",
                    "value": access,
                    "domain": ".wildberries.ru",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "lax",
                }
            )
        return json.dumps(items, ensure_ascii=False)


def _access_token_from_storage(storage: dict[str, str]) -> str | None:
    token_data = storage.get("wbx__tokenData")
    if token_data:
        try:
            access = json.loads(token_data).get("token")
        except json.JSONDecodeError:
            access = None
        if access:
            return str(access)
    oauth = storage.get("wbid-oauth-sdk-access-token")
    if oauth:
        return str(oauth)
    return None


def _buyer_token_from_storage(storage: dict[str, str]) -> str | None:
    token = _access_token_from_storage(storage)
    if token and is_buyer_api_token(token):
        return token
    return None


def _buyer_token_from_storage_state(state: dict[str, Any]) -> str | None:
    for origin in state.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        storage: dict[str, str] = {}
        for item in origin.get("localStorage") or []:
            if isinstance(item, dict) and item.get("name"):
                storage[str(item["name"])] = str(item.get("value") or "")
        token = _buyer_token_from_storage(storage)
        if token:
            return token
    return None


def _storage_state_with_token(state: dict[str, Any], access_token: str) -> dict[str, Any]:
    """Ensure Playwright storage_state contains wbx__tokenData for HA import."""
    merged = json.loads(json.dumps(state))
    token_value = json.dumps({"token": access_token})
    origins = merged.setdefault("origins", [])
    wb_origin = f"{WB_BASE}/"
    target = None
    for origin in origins:
        if isinstance(origin, dict) and "wildberries.ru" in str(origin.get("origin", "")):
            target = origin
            break
    if target is None:
        target = {"origin": wb_origin, "localStorage": []}
        origins.append(target)
    items = target.setdefault("localStorage", [])
    for item in items:
        if isinstance(item, dict) and item.get("name") == "wbx__tokenData":
            item["value"] = token_value
            return merged
    items.append({"name": "wbx__tokenData", "value": token_value})
    return merged


def _has_antibot_cookie(cookies: list[dict[str, Any]]) -> bool:
    return any(c.get("name") == "x_wbaas_token" for c in cookies)


def _extract_oauth_code(url: str) -> str | None:
    parsed = urlparse(url)
    code = parse_qs(parsed.query).get("code", [None])[0]
    if code:
        return str(code)
    if parsed.fragment:
        code = parse_qs(parsed.fragment).get("code", [None])[0]
        if code:
            return str(code)
    return None


OAUTH_REDIRECT_URI = "https://www.wildberries.ru/wb-id/callback"
WB_BFF_TOKEN_PATH = "/oauth-bff/api/v1/token"


def calc_nonce(hex_prefix: str, leading_zeroes: int) -> str:
    target = "0" * leading_zeroes
    counter = 0
    while True:
        if hashlib.sha256(f"{hex_prefix}{counter}".encode()).hexdigest().startswith(target):
            return str(counter)
        counter += 1


class WbPhoneLogin:
    """Headless WB phone login. Requires: pip install playwright && playwright install chromium."""

    def __init__(self, phone: str, *, verbose: bool = False) -> None:
        self.phone = phone.replace("+", "").replace(" ", "").replace("-", "")
        if self.phone.startswith("8") and len(self.phone) == 11:
            self.phone = "7" + self.phone[1:]
        self._verbose = verbose
        self._flow: dict[str, Any] = {}
        self._send: dict[str, Any] = {}
        self._nonce = "0"
        self._pkce_verifier = secrets.token_urlsafe(64)[:86]
        self._oauth_state = secrets.token_hex(16)
        self._oauth_codes: list[str] = []
        self._oauth_tokens: list[str] = []
        self._oauth_debug: list[str] = []
        self._exchange_attempted: set[str] = set()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    async def __aenter__(self) -> WbPhoneLogin:
        from playwright.async_api import async_playwright

        await ensure_chromium()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True,
            args=CHROMIUM_LAUNCH_ARGS,
        )
        self._context = await self._browser.new_context(
            user_agent=BROWSER_USER_AGENT,
            locale="ru-RU",
            viewport={"width": 1280, "height": 900},
        )
        await self._context.add_init_script(STEALTH_INIT)
        self._page = await self._context.new_page()

        async def on_response(response: Any) -> None:
            url = response.url
            if url.endswith("/auth/flow/start") and response.status == 200:
                self._flow = await response.json()
                self._nonce = calc_nonce(
                    self._flow["hexPrefix"],
                    int(self._flow["leadingZeroes"]),
                )
            if "send-code" in url and response.status == 200:
                self._send = await response.json()

        self._page.on("response", on_response)
        return self

    def _oauth_authorize_url(self) -> str:
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(self._pkce_verifier.encode()).digest()
        ).decode().rstrip("=")
        return (
            f"{ID_BASE}/login/oauth2/authorize?"
            f"client_id=marketplace_web&response_type=code&state={self._oauth_state}"
            f"&redirect_uri={quote(OAUTH_REDIRECT_URI, safe='')}"
            "&scope=openid%20phone%20read%3Aprofile%20read%3Aemail"
            f"&code_challenge={challenge}&code_challenge_method=S256&prompt=consent"
            "&audience=https%3A%2F%2Fwww.wildberries.ru"
        )

    def _login_page_url(self) -> str:
        return f"{ID_BASE}/login/?retPath={quote(self._oauth_authorize_url(), safe='')}"

    async def __aexit__(self, *args: object) -> None:
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()

    async def send_code(self) -> SendCodeResult:
        if not self._page:
            raise RuntimeError("use async with WbPhoneLogin(...)")

        await self._page.goto(self._login_page_url(), wait_until="domcontentloaded", timeout=90_000)
        await self._page.wait_for_timeout(3000)
        digits = self.phone[-10:] if self.phone.startswith("7") else self.phone
        await self._page.get_by_placeholder("000 000-00-00").fill(digits)
        await self._page.get_by_role("button", name="Получить код").click()

        for _ in range(40):
            if self._send:
                break
            await self._page.wait_for_timeout(500)

        if not self._send:
            raise RuntimeError("send-code failed (antibot, rate limit, or invalid phone)")

        return SendCodeResult(
            confirmation_type=str(self._send.get("confirmationType", "")),
            sticker=str(self._send.get("sticker", "")),
            flow_id=str(self._flow.get("flowId", "")),
            nonce=self._nonce,
            deny_resend_until=self._send.get("denyResendUntil"),
        )

    async def export_pending(self) -> PendingPhoneLogin:
        if not self._context or not self._send:
            raise RuntimeError("call send_code() first")
        return PendingPhoneLogin(
            phone=self.phone,
            browser_state=await self._context.storage_state(),
            flow=self._flow,
            send=self._send,
            nonce=self._nonce,
            pkce_verifier=self._pkce_verifier,
            oauth_state=self._oauth_state,
        )

    @classmethod
    async def restore(cls, pending: PendingPhoneLogin) -> WbPhoneLogin:
        login = cls(pending.phone)
        from playwright.async_api import async_playwright

        await ensure_chromium()
        login._playwright = await async_playwright().start()
        login._browser = await login._playwright.chromium.launch(
            headless=True,
            args=CHROMIUM_LAUNCH_ARGS,
        )
        login._context = await login._browser.new_context(
            user_agent=BROWSER_USER_AGENT,
            locale="ru-RU",
            viewport={"width": 1280, "height": 900},
            storage_state=pending.browser_state,
        )
        await login._context.add_init_script(STEALTH_INIT)
        login._page = await login._context.new_page()
        login._flow = pending.flow
        login._send = pending.send
        login._nonce = pending.nonce
        login._pkce_verifier = pending.pkce_verifier or secrets.token_urlsafe(64)[:86]
        login._oauth_state = pending.oauth_state or secrets.token_hex(16)
        await login._page.goto(f"{ID_BASE}/login/", wait_until="domcontentloaded", timeout=90_000)
        await login._page.wait_for_timeout(1000)
        return login

    async def close(self) -> None:
        await self.__aexit__(None, None, None)

    async def confirm_code(self, code: str) -> LoginSessionExport:
        if not self._page or not self._send:
            raise RuntimeError("call send_code() first")

        confirm_body = {
            "code": code.strip(),
            "flowId": self._flow["flowId"],
            "nonce": self._nonce,
            "sticker": self._send["sticker"],
        }
        confirm_resp = await self._page.evaluate(
            """async (body) => {
              const r = await fetch('/wb-auth/v2/auth/flow/code-confirm', {
                method: 'POST',
                credentials: 'include',
                headers: {'Content-Type': 'application/json', 'Accept': 'application/json'},
                body: JSON.stringify(body),
              });
              let json = null;
              try { json = await r.json(); } catch (e) {}
              return {status: r.status, json, text: json ? '' : await r.text()};
            }""",
            confirm_body,
        )
        if confirm_resp.get("status") != 200:
            raise RuntimeError(
                f"code-confirm HTTP {confirm_resp.get('status')}: "
                f"{confirm_resp.get('json') or confirm_resp.get('text')}"
            )

        confirm_json = confirm_resp.get("json") or {}
        redirect_to = confirm_json.get("redirectTo")
        wb_id_token = confirm_json.get("accessToken") or confirm_json.get("access_token")

        self._oauth_codes = []
        self._oauth_tokens = []
        self._oauth_debug = []
        self._exchange_attempted = set()
        self._attach_oauth_capture()

        self._oauth_note("code-confirm: OK")
        self._oauth_note(
            f"code-confirm: redirectTo={'есть' if redirect_to else 'нет'}, "
            f"wb-id token={'есть' if wb_id_token else 'нет'}"
        )
        if redirect_to:
            self._oauth_note(f"code-confirm: redirectTo={self._short_url(redirect_to, 200)}")

        self._oauth_note(
            "OAuth: wb-id → sso_token → marketplace JWT (нужен для /myorders API)"
        )
        oauth_started = time.monotonic()
        marketplace_token = await self._complete_marketplace_oauth(redirect_to, wb_id_token)
        self._oauth_note(
            f"OAuth marketplace: {'токен получен' if marketplace_token else 'токен не в ответе'} "
            f"за {time.monotonic() - oauth_started:.0f}s"
        )
        self._oauth_note("antibot: открываем wildberries.ru для x_wbaas_token…")
        await self._ensure_antibot_cookie()

        storage = await self._read_local_storage()
        if marketplace_token:
            storage["wbx__tokenData"] = json.dumps({"token": marketplace_token})
        elif not _buyer_token_from_storage(storage):
            storage = await self._poll_buyer_token(seconds=25)

        access = _buyer_token_from_storage(storage)
        if not access:
            state = await self._context.storage_state()
            access = _buyer_token_from_storage_state(state)
        else:
            state = None

        if not access:
            page_url = self._page.url if self._page else "?"
            debug = "; ".join(self._oauth_debug[-6:]) if self._oauth_debug else "n/a"
            raise RuntimeError(
                "Buyer API token not found after OAuth "
                f"(codes={len(self._oauth_codes)}, page={page_url}, "
                f"redirectTo={redirect_to!r}, debug={debug}, "
                f"localStorage keys: {sorted(storage.keys())})"
            )

        if state is None:
            state = await self._context.storage_state()
            if not _buyer_token_from_storage_state(state):
                state = _storage_state_with_token(state, access)

        cookies = await self._context.cookies()
        if not _has_antibot_cookie(cookies):
            raise RuntimeError(
                "x_wbaas_token not obtained — antibot cookie missing after visiting wildberries.ru"
            )

        return LoginSessionExport(
            cookies=cookies,
            local_storage=storage,
            storage_state=state,
        )

    def _note_oauth_url(self, url: str) -> None:
        code = _extract_oauth_code(url)
        if code and code not in self._oauth_codes:
            self._oauth_codes.append(code)

    def _attach_oauth_capture(self) -> None:
        if not self._page:
            return

        async def on_response(response: Any) -> None:
            try:
                if response.status != 200:
                    return
                url = response.url
                if "oauth-bff/api/v1/token" in url:
                    body = await response.json()
                    if isinstance(body, dict):
                        value = body.get("accessToken") or body.get("access_token")
                        if value and is_buyer_api_token(str(value)):
                            self._oauth_tokens.append(str(value))
                            self._oauth_note("перехвачен marketplace token из oauth-bff")
                            return
                if "oauth2/token" in url or (
                    "token" in url and "oauth" in url
                ):
                    body = await response.json()
                    if isinstance(body, dict):
                        for key in ("access_token", "accessToken", "token"):
                            value = body.get(key)
                            if value and is_buyer_api_token(str(value)):
                                self._oauth_tokens.append(str(value))
                                self._oauth_note("перехвачен marketplace token из oauth2/token")
                                return
                if "token" not in url and "oauth" not in url and "__internal" not in url:
                    return
                body = await response.json()
                if not isinstance(body, dict):
                    return
                for key in ("access_token", "accessToken", "token"):
                    value = body.get(key)
                    if value and is_buyer_api_token(str(value)):
                        self._oauth_tokens.append(str(value))
            except Exception:
                return

        def on_navigated(frame: Any) -> None:
            if self._page and frame == self._page.main_frame:
                self._note_oauth_url(frame.url)

        self._page.on("response", on_response)
        self._page.on("framenavigated", on_navigated)

    async def _maybe_accept_oauth_consent(self) -> None:
        if not self._page:
            return
        for label in ("Разрешить", "Продолжить", "Подтвердить", "Allow"):
            button = self._page.get_by_role("button", name=label)
            if await button.count():
                await button.first.click()
                await self._page.wait_for_timeout(2000)
                return

    def _oauth_note(self, message: str) -> None:
        self._oauth_debug.append(message)
        _LOGGER.debug("OAuth: %s", message)
        if self._verbose:
            print(f"[wb-login] {message}", file=sys.stderr, flush=True)

    def _short_url(self, url: str | None, limit: int = 120) -> str:
        if not url:
            return "—"
        return url if len(url) <= limit else url[: limit - 3] + "..."

    async def _read_buyer_token_from_context(self) -> str | None:
        if self._context:
            state = await self._context.storage_state()
            token = _buyer_token_from_storage_state(state)
            if token:
                return token
        storage = await self._read_local_storage()
        return _buyer_token_from_storage(storage)

    async def _try_exchange_code(
        self, code: str, *, oauth_state: str | None = None
    ) -> str | None:
        if code in self._exchange_attempted:
            return None
        self._exchange_attempted.add(code)

        state = oauth_state or self._oauth_state
        for fn, kwargs in (
            (self._exchange_code_via_wb_bff, {"code": code, "oauth_state": state}),
            (self._exchange_oauth_code, {"code": code}),
            (self._exchange_oauth_code_on_id_origin, {"code": code}),
            (self._open_callback_and_wait, {"code": code, "oauth_state": state}),
        ):
            self._oauth_note(f"обмен code→token: {fn.__name__}…")
            try:
                token = await fn(**kwargs)
            except Exception as err:
                self._oauth_note(f"{fn.__name__}: {err}")
                token = None
            if token:
                self._oauth_note(f"обмен code→token: OK ({fn.__name__})")
                return token
            self._oauth_note(f"обмен code→token: нет ({fn.__name__})")
        return None

    async def _ensure_wildberries_page(self) -> None:
        if not self._page:
            return
        if "wildberries.ru" in self._page.url:
            return
        await self._page.goto(
            f"{WB_BASE}/", wait_until="domcontentloaded", timeout=45_000
        )

    async def _seed_pkce_session_storage(self, oauth_state: str) -> None:
        """WB ID SDK reads codeVerifier from sessionStorage[state] on callback."""
        if not self._page or not oauth_state:
            return
        await self._ensure_wildberries_page()
        await self._page.evaluate(
            """([state, verifier]) => {
              sessionStorage.setItem(state, verifier);
            }""",
            [oauth_state, self._pkce_verifier],
        )

    async def _persist_marketplace_token(self, access_token: str) -> None:
        if not self._page:
            return
        await self._page.evaluate(
            """(token) => {
              localStorage.setItem('wbid-oauth-sdk-access-token', token);
              localStorage.setItem('wbx__tokenData', JSON.stringify({token}));
            }""",
            access_token,
        )

    async def _exchange_code_via_wb_bff(
        self, code: str, *, oauth_state: str | None = None
    ) -> str | None:
        """Exchange authorization code via wildberries.ru BFF (same as wb-id SDK)."""
        if not self._page:
            return None

        state = oauth_state or self._oauth_state
        await self._seed_pkce_session_storage(state)
        result = await self._page.evaluate(
            """async ({code, state, verifier, redirectUri}) => {
              try {
                const r = await fetch('/oauth-bff/api/v1/token', {
                  method: 'POST',
                  credentials: 'include',
                  headers: {
                    'Content-Type': 'application/json',
                    Accept: 'application/json',
                  },
                  body: JSON.stringify({
                    clientId: 'marketplace_web',
                    code,
                    grantType: 'authorization_code',
                    codeVerifier: verifier,
                    redirectUri,
                    state,
                  }),
                });
                let json = null;
                try { json = await r.json(); } catch (e) {}
                return {
                  status: r.status,
                  accessToken: json?.accessToken || json?.access_token,
                  json,
                };
              } catch (e) {
                return {status: 0, error: String(e)};
              }
            }""",
            {
                "code": code,
                "state": state,
                "verifier": self._pkce_verifier,
                "redirectUri": OAUTH_REDIRECT_URI,
            },
        )
        token = (result or {}).get("accessToken")
        if token and is_buyer_api_token(str(token)):
            await self._persist_marketplace_token(str(token))
            self._oauth_note(
                f"oauth-bff/token: OK HTTP {(result or {}).get('status')}"
            )
            return str(token)
        self._oauth_note(
            f"oauth-bff/token HTTP {(result or {}).get('status')}: "
            f"{str((result or {}).get('json') or (result or {}).get('error'))[:160]}"
        )
        return None

    def _with_fresh_auth(self, url: str) -> str:
        if not url:
            return url
        if not urlparse(url).scheme:
            url = urljoin(f"{ID_BASE}/", url.lstrip("/"))
        parsed = urlparse(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        query["fresh_auth"] = ["1"]
        flat = {k: (v[0] if len(v) == 1 else v) for k, v in query.items()}
        return urlunparse(parsed._replace(query=urlencode(flat)))

    async def _inject_wb_id_token(self, token: str) -> None:
        if not self._page:
            return
        await self._page.evaluate(
            """(token) => {
              localStorage.setItem('wbIdAccessToken', token);
            }""",
            token,
        )

    async def _browser_oauth_redirect(
        self, redirect_to: str, wb_id_token: str | None
    ) -> str | None:
        if not self._page or not redirect_to:
            return None
        if wb_id_token:
            await self._inject_wb_id_token(wb_id_token)
        url = self._with_fresh_auth(redirect_to)
        self._oauth_note(f"browser redirect → {url[:160]}")
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        except Exception as err:
            self._oauth_note(f"redirect navigation error: {err}")
        self._oauth_note(f"after redirect page={self._page.url[:160]}")
        await self._maybe_accept_oauth_consent()
        token = await self._wait_for_buyer_token_on_page(45)
        if token:
            return token
        code, api_redirect = await self._oauth_authorize_api(redirect_to, wb_id_token)
        if code:
            self._note_oauth_url(f"{OAUTH_REDIRECT_URI}?code={code}")
            token = await self._try_exchange_code(code)
            if token:
                return token
        if api_redirect and self._page:
            self._oauth_note(f"authorize API redirect → {str(api_redirect)[:160]}")
            await self._page.goto(
                str(api_redirect), wait_until="domcontentloaded", timeout=60_000
            )
            return await self._wait_for_buyer_token_on_page(40)
        return None

    async def _wb_auth_sso_exchange(self, redirect_to: str) -> str | None:
        if not self._page:
            return None
        params = self._authorize_params_from_url(redirect_to)
        if not params:
            return None
        if "id.wb.ru" not in self._page.url:
            await self._page.goto(f"{ID_BASE}/login/", wait_until="domcontentloaded", timeout=60_000)

        result = await self._page.evaluate(
            """async (params) => {
              try {
                const r = await fetch(
                  '/wb-auth/v2/auth/sso/exchange?' + new URLSearchParams(params),
                  {credentials: 'include', headers: {Accept: 'application/json'}}
                );
                let json = null;
                try { json = await r.json(); } catch (e) {}
                const redirectUrl = json?.redirectTo || json?.redirectURL || json?.url;
                const token = json?.access_token || json?.accessToken || json?.token;
                return {status: r.status, redirectUrl, token, json};
              } catch (e) {
                return {status: 0, error: String(e)};
              }
            }""",
            params,
        )
        self._oauth_note(f"sso/exchange status={(result or {}).get('status')}")
        token = (result or {}).get("token")
        if token and is_buyer_api_token(str(token)):
            return str(token)
        redirect_url = (result or {}).get("redirectUrl")
        if redirect_url and self._page:
            await self._page.goto(
                str(redirect_url), wait_until="domcontentloaded", timeout=60_000
            )
            return await self._wait_for_buyer_token_on_page(40)
        return None

    def _authorize_params_from_url(self, authorize_url: str) -> dict[str, str]:
        parsed = urlparse(authorize_url)
        return {k: v[0] for k, v in parse_qs(parsed.query).items() if v}

    def _verify_pkce(self, authorize_url: str) -> None:
        params = self._authorize_params_from_url(authorize_url)
        challenge = params.get("code_challenge")
        if not challenge:
            self._oauth_note("PKCE: code_challenge нет в redirectTo — обмен без verifier")
            return
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(self._pkce_verifier.encode()).digest()
        ).decode().rstrip("=")
        if challenge == expected:
            self._oauth_note("PKCE: code_challenge совпадает с нашим verifier")
        else:
            self._oauth_note(
                "PKCE: code_challenge НЕ совпадает — возможен invalid_grant при обмене"
            )

    def _state_from_authorize_url(self, authorize_url: str | None) -> str:
        if not authorize_url:
            return self._oauth_state
        state = parse_qs(urlparse(authorize_url).query).get("state", [None])[0]
        return str(state) if state else self._oauth_state

    def _jwt_client_id(self, token: str) -> str:
        payload = _decode_jwt_payload(token) or {}
        return str(payload.get("client_id") or payload.get("clientId") or "?")

    async def _fetch_sso_token(self, wb_id_token: str) -> str | None:
        """Exchange wb-id access token for SSO session token (next OAuth step)."""
        if not self._page:
            return None
        if "id.wb.ru" not in self._page.url:
            await self._page.goto(
                f"{ID_BASE}/login/", wait_until="domcontentloaded", timeout=30_000
            )
        try:
            result = await self._page.evaluate(
                """async (accessToken) => {
                  try {
                    const r = await fetch('/oauth/v2/sso/wb-id/token', {
                      method: 'POST',
                      credentials: 'include',
                      headers: {'Content-Type': 'application/json', Accept: 'application/json'},
                      body: JSON.stringify({access_token: accessToken}),
                    });
                    let json = null;
                    try { json = await r.json(); } catch (e) {}
                    return {status: r.status, json};
                  } catch (e) {
                    return {status: 0, error: String(e)};
                  }
                }""",
                wb_id_token,
            )
        except Exception as err:
            self._oauth_note(f"sso/wb-id fetch error: {err}")
            return None
        data = (result or {}).get("json") or {}
        sso = data.get("sso_token") or data.get("ssoToken")
        if sso:
            self._oauth_note(
                f"sso_token получен (client_id={self._jwt_client_id(str(sso))})"
            )
            return str(sso)
        self._oauth_note(
            f"sso/wb-id HTTP {(result or {}).get('status')}: "
            f"{str(data or (result or {}).get('error'))[:160]}"
        )
        return None

    async def _oauth_authorize_and_exchange(
        self, authorize_url: str, bearer_token: str, *, label: str
    ) -> str | None:
        """Authorize + token exchange in one browser session (same cookies)."""
        if not self._page:
            return None
        if "id.wb.ru" not in self._page.url:
            await self._page.goto(
                f"{ID_BASE}/login/", wait_until="domcontentloaded", timeout=30_000
            )

        params = self._authorize_params_from_url(authorize_url)
        result = await self._page.evaluate(
            """async ({params, bearer, verifier, redirectUri}) => {
              try {
                const authHeaders = {Accept: 'application/json', Authorization: 'Bearer ' + bearer};
                let authResp = await fetch(
                  '/oauth/v2/oauth2/authorize?' + new URLSearchParams(params),
                  {credentials: 'include', headers: authHeaders}
                );
                let authJson = null;
                try { authJson = await authResp.json(); } catch (e) {}

                if (authJson?.consentChallenge) {
                  const acceptResp = await fetch('/oauth/v2/oauth/consent/accept', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json', Accept: 'application/json', ...authHeaders},
                    body: JSON.stringify({consentChallenge: authJson.consentChallenge}),
                  });
                  let acceptJson = null;
                  try { acceptJson = await acceptResp.json(); } catch (e) {}
                  const next = {...params};
                  const consentVerifier =
                    acceptJson?.consentVerifier || acceptJson?.consent_verifier;
                  if (consentVerifier) next.consent_verifier = consentVerifier;
                  authResp = await fetch(
                    '/oauth/v2/oauth2/authorize?' + new URLSearchParams(next),
                    {credentials: 'include', headers: authHeaders}
                  );
                  try { authJson = await authResp.json(); } catch (e) { authJson = null; }
                }

                const redirectUrl = authJson?.url || authJson?.redirectURL || authJson?.redirectTo;
                const code = redirectUrl ? new URL(redirectUrl).searchParams.get('code') : null;
                if (!code) {
                  return {step: 'authorize', status: authResp.status, redirectUrl, authJson};
                }

                return {step: 'redirect', code, redirectUrl, status: authResp.status};
              } catch (e) {
                return {error: String(e)};
              }
            }""",
            {
                "params": params,
                "bearer": bearer_token,
                "verifier": self._pkce_verifier,
                "redirectUri": OAUTH_REDIRECT_URI,
            },
        )
        if (result or {}).get("error"):
            self._oauth_note(f"authorize+token ({label}): {(result or {}).get('error')}")
            return None
        if (result or {}).get("step") == "authorize":
            auth_json = (result or {}).get("authJson") or {}
            hint = ""
            if auth_json.get("consentChallenge"):
                hint = ", consentChallenge без consent_verifier"
            self._oauth_note(
                f"authorize ({label}): нет code, status={(result or {}).get('status')}{hint}"
            )
            return None

        redirect_url = (result or {}).get("redirectUrl")
        code = (result or {}).get("code")
        if code and redirect_url:
            oauth_state = self._state_from_authorize_url(authorize_url)
            self._oauth_note(
                f"authorize ({label}): code получен, oauth-bff… "
                f"HTTP {(result or {}).get('status')}"
            )
            self._note_oauth_url(str(redirect_url))
            token = await self._try_exchange_code(
                str(code), oauth_state=oauth_state
            )
            if token:
                return token
            if self._page:
                await self._seed_pkce_session_storage(oauth_state)
                try:
                    await self._page.goto(
                        str(redirect_url),
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                except Exception as err:
                    self._oauth_note(f"callback navigation: {err}")
                token = await self._wait_for_buyer_token_on_page(20, oauth_state=oauth_state)
                if token:
                    return token
        self._oauth_note(f"authorize ({label}): marketplace token не получен")
        return None

    async def _browser_fresh_auth_finish(
        self, redirect_to: str, wb_id_token: str | None, sso_token: str | None
    ) -> str | None:
        """Mimic browser: fresh_auth redirect after phone login."""
        if not self._page or not redirect_to:
            return None
        if wb_id_token:
            await self._inject_wb_id_token(wb_id_token)
        url = self._with_fresh_auth(redirect_to)
        self._oauth_note(f"browser fresh_auth → {self._short_url(url, 140)}")
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        except Exception as err:
            self._oauth_note(f"fresh_auth navigation: {err}")
        await self._maybe_accept_oauth_consent()
        token = await self._wait_for_buyer_token_on_page(25)
        if token:
            return token
        code, api_redirect = await self._oauth_authorize_api(redirect_to, wb_id_token)
        if api_redirect and self._page:
            self._oauth_note(f"fresh_auth: authorize API → {self._short_url(str(api_redirect))}")
            await self._page.goto(
                str(api_redirect), wait_until="domcontentloaded", timeout=60_000
            )
            token = await self._wait_for_buyer_token_on_page(35)
            if token:
                return token
        if code:
            self._note_oauth_url(f"{OAUTH_REDIRECT_URI}?code={code}")
            token = await self._try_exchange_code(code)
            if token:
                return token
        code = _extract_oauth_code(self._page.url)
        if not code and self._oauth_codes:
            code = self._oauth_codes[-1]
        if code and sso_token:
            return await self._oauth_authorize_and_exchange(
                redirect_to, sso_token, label="sso-retry"
            )
        return None

    async def _sso_wb_id_token_exchange(self, wb_id_token: str) -> str | None:
        if not self._context:
            return None

        url = f"{ID_BASE}/oauth/v2/sso/wb-id/token"
        bodies = [
            {"access_token": wb_id_token},
            {"access_token": wb_id_token, "client_id": "marketplace_web"},
        ]
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": ID_BASE,
            "Referer": f"{ID_BASE}/login/",
        }
        for body in bodies:
            try:
                response = await self._context.request.post(
                    url, data=json.dumps(body), headers=headers
                )
            except Exception as err:
                self._oauth_note(f"sso/wb-id request error: {err}")
                continue
            text = await response.text()
            if not response.ok:
                self._oauth_note(f"sso/wb-id HTTP {response.status}: {text[:160]}")
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            token = data.get("access_token") or data.get("accessToken") or data.get("token")
            if token and is_buyer_api_token(str(token)):
                self._oauth_note("sso/wb-id: marketplace token получен")
                return str(token)
            self._oauth_note(f"sso/wb-id: ответ без buyer token: {text[:160]}")

        if self._page and "id.wb.ru" in self._page.url:
            try:
                result = await self._page.evaluate(
                    """async (accessToken) => {
                      try {
                        const r = await fetch('/oauth/v2/sso/wb-id/token', {
                          method: 'POST',
                          credentials: 'include',
                          headers: {
                            'Content-Type': 'application/json',
                            Accept: 'application/json',
                          },
                          body: JSON.stringify({access_token: accessToken}),
                        });
                        let json = null;
                        try { json = await r.json(); } catch (e) {}
                        const token = json?.access_token || json?.accessToken || json?.token;
                        return {status: r.status, token, json};
                      } catch (e) {
                        return {status: 0, error: String(e)};
                      }
                    }""",
                    wb_id_token,
                )
            except Exception as err:
                self._oauth_note(f"sso/wb-id page fetch error: {err}")
                result = None
            token = (result or {}).get("token")
            if token and is_buyer_api_token(str(token)):
                return str(token)
            if result:
                self._oauth_note(
                    f"sso/wb-id page HTTP {(result or {}).get('status')}: "
                    f"{str((result or {}).get('json') or (result or {}).get('error'))[:160]}"
                )
        return None

    async def _oauth_authorize_api(
        self, authorize_url: str, wb_id_token: str | None = None
    ) -> tuple[str | None, str | None]:
        """Return (authorization_code, redirect_url) via id.wb.ru OAuth API."""
        if not self._page:
            return None, None
        if "id.wb.ru" not in self._page.url:
            await self._page.goto(f"{ID_BASE}/login/", wait_until="domcontentloaded", timeout=60_000)

        params = self._authorize_params_from_url(authorize_url)
        if not params.get("client_id"):
            return None, None

        result = await self._page.evaluate(
            """async ({params, wbToken}) => {
              try {
                const base = 'https://id.wb.ru';
                const headers = {Accept: 'application/json'};
                if (wbToken) headers.Authorization = 'Bearer ' + wbToken;
                const authUrl = base + '/oauth/v2/oauth2/authorize?' + new URLSearchParams(params);

                let authResp = await fetch(authUrl, {credentials: 'include', headers});
                let authJson = null;
                try { authJson = await authResp.json(); } catch (e) {}

                if (authJson?.consentChallenge) {
                  const acceptResp = await fetch(base + '/oauth/v2/oauth/consent/accept', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {'Content-Type': 'application/json', Accept: 'application/json', ...headers},
                    body: JSON.stringify({consentChallenge: authJson.consentChallenge}),
                  });
                  let acceptJson = null;
                  try { acceptJson = await acceptResp.json(); } catch (e) {}
                  const next = {...params};
                  if (acceptJson?.consentVerifier) next.consent_verifier = acceptJson.consentVerifier;
                  if (acceptJson?.consent_verifier) next.consent_verifier = acceptJson.consent_verifier;
                  authResp = await fetch(
                    base + '/oauth/v2/oauth2/authorize?' + new URLSearchParams(next),
                    {credentials: 'include', headers}
                  );
                  try { authJson = await authResp.json(); } catch (e) { authJson = null; }
                }

                const redirectUrl = authJson?.url || authJson?.redirectURL || authJson?.redirectTo;
                return {
                  status: authResp.status,
                  redirectUrl: redirectUrl || null,
                  json: authJson,
                };
              } catch (e) {
                return {status: 0, error: String(e)};
              }
            }""",
            {"params": params, "wbToken": wb_id_token},
        )
        redirect_note = str((result or {}).get("redirectUrl") or "")[:120]
        self._oauth_note(
            f"authorize API status={(result or {}).get('status')} "
            f"redirect={redirect_note or 'none'}"
        )
        redirect_url = (result or {}).get("redirectUrl")
        if redirect_url:
            code = _extract_oauth_code(str(redirect_url))
            return code, str(redirect_url)
        return None, None

    async def _wait_for_buyer_token_on_page(
        self, seconds: int, *, oauth_state: str | None = None
    ) -> str | None:
        if not self._page:
            return None

        state = oauth_state or self._oauth_state
        url = self._page.url
        code = _extract_oauth_code(url)
        on_callback = "wb-id/callback" in url
        if on_callback and code and state:
            await self._seed_pkce_session_storage(state)
        spa_wait = min(10, seconds) if on_callback and code else 0

        if on_callback and code:
            self._oauth_note(
                f"callback: ждём SPA до {spa_wait}s, code={code[:8]}…"
            )

        for i in range(seconds * 2):
            url = self._page.url
            self._note_oauth_url(url)
            if self._verbose and i > 0 and i % 10 == 0:
                self._oauth_note(
                    f"ожидание токена {i // 2}/{seconds}s | "
                    f"url={self._short_url(url)} | codes={len(self._oauth_codes)}"
                )
            token = await self._read_buyer_token_from_context()
            if token:
                return token
            if self._oauth_tokens:
                return self._oauth_tokens[-1]

            current_code = _extract_oauth_code(url)
            if current_code and i >= spa_wait * 2:
                token = await self._try_exchange_code(
                    current_code, oauth_state=state
                )
                if token:
                    return token

            await self._page.wait_for_timeout(500)
        return None

    async def _complete_marketplace_oauth(
        self, redirect_to: str | None, wb_id_token: str | None
    ) -> str | None:
        if self._oauth_tokens:
            return self._oauth_tokens[-1]

        if wb_id_token:
            await self._inject_wb_id_token(wb_id_token)

        sso_token: str | None = None
        if wb_id_token:
            self._oauth_note("① sso/wb-id/token → sso_token")
            sso_token = await self._fetch_sso_token(wb_id_token)

        if not redirect_to:
            return None

        self._verify_pkce(redirect_to)

        for bearer, label in (
            (wb_id_token, "wb-id"),
            (sso_token, "sso_token"),
        ):
            if not bearer:
                continue
            self._oauth_note(f"② authorize → callback SPA (Bearer {label})")
            token = await self._oauth_authorize_and_exchange(
                redirect_to, bearer, label=label
            )
            if token:
                return token

        self._oauth_note("③ browser fresh_auth (как после code-confirm)")
        token = await self._browser_fresh_auth_finish(
            redirect_to, wb_id_token, sso_token
        )
        if token:
            return token

        self._oauth_note("④ callback на wildberries.ru (если code уже перехвачен)")
        if self._oauth_codes and self._page:
            oauth_state = self._state_from_authorize_url(redirect_to)
            code = self._oauth_codes[-1]
            callback = (
                f"{OAUTH_REDIRECT_URI}?code={quote(code)}"
                f"&state={quote(oauth_state)}"
            )
            await self._page.goto(
                callback, wait_until="domcontentloaded", timeout=30_000
            )
            token = await self._wait_for_buyer_token_on_page(15)
            if token:
                return token

        if self._oauth_tokens:
            return self._oauth_tokens[-1]
        return None

    async def _fetch_oauth_code_via_request(self, authorize_url: str) -> str | None:
        if not self._context:
            return None
        try:
            response = await self._context.request.get(authorize_url, max_redirects=20)
        except Exception as err:
            _LOGGER.debug("OAuth authorize request failed: %s", err)
            return None
        return _extract_oauth_code(response.url)

    async def _open_callback_and_wait(
        self, code: str, *, oauth_state: str | None = None
    ) -> str | None:
        if not self._page:
            return None
        state = oauth_state or self._oauth_state
        callback = (
            f"{OAUTH_REDIRECT_URI}?code={quote(code)}"
            f"&state={quote(state)}"
        )
        await self._seed_pkce_session_storage(state)
        if _extract_oauth_code(self._page.url) != code:
            await self._page.goto(callback, wait_until="domcontentloaded", timeout=60_000)
        for _ in range(60):
            token = await self._read_buyer_token_from_context()
            if token:
                return token
            if self._oauth_tokens:
                return self._oauth_tokens[-1]
            await self._page.wait_for_timeout(500)
        return None

    async def _read_buyer_token_from_pages(self) -> str | None:
        if not self._page:
            return None
        for url in (
            f"{WB_BASE}/wb-id/callback",
            f"{WB_BASE}/lk/myorders/delivery",
            f"{WB_BASE}/",
        ):
            await self._page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            for _ in range(20):
                token = await self._read_buyer_token_from_context()
                if token:
                    return token
                code = _extract_oauth_code(self._page.url)
                if code:
                    try:
                        token = await self._try_exchange_code(code)
                    except Exception as err:
                        self._oauth_note(f"read pages exchange: {err}")
                        token = None
                    if token:
                        return token
                await self._page.wait_for_timeout(1000)
        return None

    async def _exchange_oauth_code(self, code: str) -> str | None:
        if not self._context:
            return None
        url = f"{ID_BASE}/oauth/v2/oauth2/token"
        base = {
            "grantType": "authorization_code",
            "clientId": "marketplace_web",
            "code": code,
            "redirectUri": OAUTH_REDIRECT_URI,
            "codeVerifier": self._pkce_verifier,
        }
        payloads = [
            base,
            {k: v for k, v in base.items() if k != "codeVerifier"},
        ]
        header_sets = [
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": WB_BASE,
                "Referer": f"{WB_BASE}/wb-id/callback",
            },
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": ID_BASE,
                "Referer": f"{ID_BASE}/login/",
            },
        ]
        for payload in payloads:
            label = "pkce" if "codeVerifier" in payload else "no-pkce"
            for hdr_idx, headers in enumerate(header_sets):
                try:
                    response = await self._context.request.post(
                        url, data=json.dumps(payload), headers=headers
                    )
                except Exception as err:
                    self._oauth_note(f"token POST ({label}#{hdr_idx}): {err}")
                    continue
                text = await response.text()
                if not response.ok:
                    self._oauth_note(
                        f"token POST ({label}#{hdr_idx}) HTTP {response.status}: {text[:160]}"
                    )
                    continue
                try:
                    body = json.loads(text)
                except json.JSONDecodeError:
                    continue
                token = body.get("access_token") or body.get("accessToken")
                if token and is_buyer_api_token(str(token)):
                    self._oauth_note(f"token POST ({label}#{hdr_idx}): OK")
                    return str(token)
        return None

    async def _exchange_oauth_code_on_id_origin(self, code: str) -> str | None:
        """Exchange authorization code via same-origin fetch on id.wb.ru."""
        if not self._page:
            return None
        if "id.wb.ru" not in self._page.url:
            await self._page.goto(
                f"{ID_BASE}/login/", wait_until="domcontentloaded", timeout=45_000
            )

        token_url = f"{ID_BASE}/oauth/v2/oauth2/token"
        payloads = [
            {
                "grant_type": "authorization_code",
                "client_id": "marketplace_web",
                "code": code,
                "redirect_uri": OAUTH_REDIRECT_URI,
                "code_verifier": self._pkce_verifier,
            },
            {
                "grantType": "authorization_code",
                "clientId": "marketplace_web",
                "code": code,
                "redirectUri": OAUTH_REDIRECT_URI,
                "codeVerifier": self._pkce_verifier,
            },
        ]
        for payload in payloads:
            try:
                result = await self._page.evaluate(
                    """async ({payload, tokenUrl}) => {
                      try {
                        for (const [contentType, body] of [
                          ['application/json', JSON.stringify(payload)],
                          ['application/x-www-form-urlencoded', new URLSearchParams(payload).toString()],
                        ]) {
                          const r = await fetch(tokenUrl, {
                            method: 'POST',
                            credentials: 'include',
                            headers: {Accept: 'application/json', 'Content-Type': contentType},
                            body,
                          });
                          let json = null;
                          try { json = await r.json(); } catch (e) {}
                          const token = json?.access_token || json?.accessToken;
                          return {ok: r.ok, status: r.status, token, body: json};
                        }
                        return {ok: false};
                      } catch (e) {
                        return {ok: false, error: String(e)};
                      }
                    }""",
                    {"payload": payload, "tokenUrl": token_url},
                )
            except Exception as err:
                self._oauth_note(f"id-origin exchange evaluate: {err}")
                continue
            if not (result or {}).get("ok"):
                detail = (result or {}).get("body") or (result or {}).get("error")
                self._oauth_note(
                    f"id-origin exchange HTTP {(result or {}).get('status')}: "
                    f"{str(detail)[:160]}"
                )
                continue
            token = (result or {}).get("token")
            if token and is_buyer_api_token(str(token)):
                return str(token)
        return None

    async def _exchange_oauth_code_via_page(self, code: str) -> str | None:
        """Deprecated: use _exchange_oauth_code_on_id_origin to avoid cross-origin fetch."""
        return await self._exchange_oauth_code_on_id_origin(code)

    async def _ensure_antibot_cookie(self, timeout_seconds: int = 45) -> None:
        """Open wildberries.ru so antibot JS sets x_wbaas_token in the browser context."""
        if not self._page or not self._context:
            return

        cookies = await self._context.cookies()
        if _has_antibot_cookie(cookies):
            self._oauth_note("antibot: x_wbaas_token уже есть")
            return

        self._oauth_note(f"antibot: {WB_BASE}/lk/myorders/delivery …")
        await self._page.goto(
            f"{WB_BASE}/lk/myorders/delivery",
            wait_until="domcontentloaded",
            timeout=60_000,
        )
        for i in range(timeout_seconds):
            cookies = await self._context.cookies()
            if _has_antibot_cookie(cookies):
                self._oauth_note("antibot: x_wbaas_token получен")
                return
            if self._verbose and i > 0 and i % 5 == 0:
                self._oauth_note(f"antibot: ждём cookie… {i}/{timeout_seconds}s")
            await self._page.wait_for_timeout(1000)
        self._oauth_note("antibot: таймаут, x_wbaas_token не появился")

    async def _poll_buyer_token(self, seconds: int) -> dict[str, str]:
        storage: dict[str, str] = {}
        for _ in range(seconds):
            if await self._read_buyer_token_from_context():
                storage = await self._read_local_storage()
                if self._context:
                    state = await self._context.storage_state()
                    for origin in state.get("origins") or []:
                        if not isinstance(origin, dict):
                            continue
                        if "wildberries.ru" not in str(origin.get("origin", "")):
                            continue
                        for item in origin.get("localStorage") or []:
                            if isinstance(item, dict) and item.get("name"):
                                storage[str(item["name"])] = str(item.get("value") or "")
                return storage
            await self._page.wait_for_timeout(1000)
        return storage

    async def _read_local_storage(self) -> dict[str, str]:
        if not self._page:
            return {}
        return await self._page.evaluate(
            """() => {
              const out = {};
              for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                if (k) out[k] = localStorage.getItem(k);
              }
              return out;
            }"""
        )

    async def login(self, code: str) -> LoginSessionExport:
        await self.send_code()
        return await self.confirm_code(code)
