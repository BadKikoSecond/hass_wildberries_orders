#!/usr/bin/env python3
"""CLI: WB phone login → export cookies JSON for Home Assistant."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_phone_login():
    api_dir = ROOT / "custom_components" / "wildberries_orders" / "api"
    for name in ("cookies", "phone_login"):
        path = api_dir / f"{name}.py"
        module_name = f"wildberries_orders_api.{name}"
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(module_name, path)
        mod = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    return sys.modules["wildberries_orders_api.phone_login"].WbPhoneLogin

WbPhoneLogin = _load_phone_login()


async def _run(phone: str, code: str | None, out: Path | None, interactive: bool, *, verbose: bool) -> int:
    async with WbPhoneLogin(phone, verbose=verbose) as login:
        sent = await login.send_code()
        print(
            json.dumps(
                {
                    "confirmation_type": sent.confirmation_type,
                    "flow_id": sent.flow_id,
                    "message": (
                        "Код отправлен. Подтвердите PUSH в приложении WB или дождитесь SMS."
                        if sent.confirmation_type == "PUSH"
                        else "Код отправлен по SMS."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not code and interactive:
            code = input("Введите 6-значный код: ").strip()
        if not code:
            print(
                "\nСессия открыта только в этом процессе. Запустите одной командой:\n"
                f"  .venv/bin/python scripts/wb_phone_login.py {phone} --code 123456 -o cookie.json",
                file=sys.stderr,
            )
            return 1

        print("Подтверждаем код…", file=sys.stderr, flush=True)
        try:
            session = await login.confirm_code(code)
        except Exception as err:
            debug = getattr(login, "_oauth_debug", None) or []
            if debug:
                print("OAuth debug: " + "; ".join(debug[-10:]), file=sys.stderr)
            raise err
        print("Сессия получена, сохраняем…", file=sys.stderr, flush=True)
        payload = session.to_cookies_json()
        has_token = "WBTokenV3" in payload or "wbx__tokenData" in payload
        if not has_token:
            print("Предупреждение: WBTokenV3 не найден в экспорте", file=sys.stderr)
        if out:
            out.write_text(payload, encoding="utf-8")
            print(f"saved → {out}")
        else:
            print(payload)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="WB phone login (headless Playwright)")
    parser.add_argument("phone", help="Phone, e.g. 79117108265")
    parser.add_argument("--code", help="Код из PUSH/SMS (6 цифр)")
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Спросить код в терминале после отправки (рекомендуется)",
    )
    parser.add_argument("-o", "--output", type=Path, help="Write cookie JSON to file")
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Без подробного лога OAuth ([wb-login])",
    )
    args = parser.parse_args()
    interactive = args.interactive or (args.code is None and sys.stdin.isatty())
    verbose = not args.quiet
    raise SystemExit(
        asyncio.run(_run(args.phone, args.code, args.output, interactive, verbose=verbose))
    )


if __name__ == "__main__":
    main()
