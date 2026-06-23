"""Sync wrappers for HA executor — Playwright must not run on the event loop."""

from __future__ import annotations

import asyncio
from typing import Any


def run_send_code(phone: str) -> dict[str, Any]:
    async def _inner() -> dict[str, Any]:
        from .phone_login import WbPhoneLogin

        async with WbPhoneLogin(phone) as login:
            sent = await login.send_code()
            pending = await login.export_pending()
        return {
            "phone": pending.phone,
            "browser_state": pending.browser_state,
            "flow": pending.flow,
            "send": pending.send,
            "nonce": pending.nonce,
            "pkce_verifier": pending.pkce_verifier,
            "oauth_state": pending.oauth_state,
            "confirmation_type": sent.confirmation_type,
        }

    return asyncio.run(_inner())


def run_confirm_code(pending_dict: dict[str, Any], code: str) -> str:
    async def _inner() -> str:
        from .phone_login import PendingPhoneLogin, WbPhoneLogin

        pending = PendingPhoneLogin(
            phone=pending_dict["phone"],
            browser_state=pending_dict["browser_state"],
            flow=pending_dict["flow"],
            send=pending_dict["send"],
            nonce=pending_dict["nonce"],
            pkce_verifier=pending_dict.get("pkce_verifier", ""),
            oauth_state=pending_dict.get("oauth_state", ""),
        )
        login = await WbPhoneLogin.restore(pending)
        try:
            session = await login.confirm_code(code)
        finally:
            await login.close()
        return session.to_cookies_json()

    return asyncio.run(_inner())
