"""Link the shop's WhatsApp Business number to Evolution API.

    docker compose run --rm whatsapp-adapter python login.py          # QR code in this terminal (default)
    docker compose run --rm whatsapp-adapter python login.py --code   # 8-character pairing code instead

QR: WhatsApp Business -> Linked devices -> Link a device -> scan the code below (it refreshes by itself).
Code: ... -> Link a device -> "Link with phone number instead" -> type the code.
A half-done link (state "connecting") is reset first: its old code would be rejected by the phone.
Nothing secret is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys

import qrcode

from app.config import EvolutionApi
from app.evolution import EvolutionClient, EvolutionError

WAIT_SECONDS = 180


def print_qr(data: str) -> None:
    qr = qrcode.QRCode(border=2)
    qr.add_data(data)
    qr.make(fit=True)
    print("\033[2J\033[H", end="")  # clear the terminal so a refreshed code replaces the old one
    qr.print_ascii(invert=True)
    print("WhatsApp Business -> Linked devices -> Link a device -> scan. The code refreshes by itself.")


async def wait_open(evo: EvolutionClient, on_tick=None) -> bool:
    for _ in range(WAIT_SECONDS // 3):
        if await evo.connection_state() == "open":
            return True
        if on_tick is not None:
            await on_tick()
        await asyncio.sleep(3)
    return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", action="store_true", help="pairing code instead of a QR code")
    args = ap.parse_args()

    api = EvolutionApi.from_env()
    evo = EvolutionClient(api.url, api.api_key, api.instance)
    try:
        created = await evo.ensure_instance(api.webhook_url, api.webhook_secret)
        print(f"Instance '{api.instance}': {'created' if created else 'exists'}, webhook set.")
        state = await evo.connection_state()
        if state == "open":
            print("Already linked (state: open). Nothing to do.")
            return 0
        if state == "connecting":
            print("Resetting a half-done link...")
            try:
                await evo.logout()
            except EvolutionError:
                pass
            await asyncio.sleep(3)

        if args.code:
            raw = os.environ.get("WA_PHONE") or input("WhatsApp Business number (+380...): ")
            digits = re.sub(r"\D", "", raw)
            if len(digits) < 10:
                print("Invalid number.")
                return 1
            data = await evo.connect(digits)
            code = (data or {}).get("pairingCode")
            if not code:
                print("No pairing code returned. Try again in a minute, or run without --code for a QR code.")
                return 1
            print(f"\nPairing code:  {code[:4]}-{code[4:]}\n")
            print("Phone: WhatsApp Business -> Linked devices -> Link a device -> Link with phone number instead.")
            linked = await wait_open(evo)
        else:
            shown = {"code": None}

            async def refresh() -> None:
                data = await evo.connect()
                code = (data or {}).get("code")
                if code and code != shown["code"]:
                    shown["code"] = code
                    print_qr(code)

            await refresh()
            if shown["code"] is None:
                print("No QR code returned yet; waiting...")
            linked = await wait_open(evo, refresh)

        if linked:
            print("\nLinked. State: open.")
            return 0
        print(f"\nNot linked within {WAIT_SECONDS // 60} minutes. Run login.py again.")
        return 1
    except EvolutionError as exc:
        detail = exc.detail.get("response", exc.detail) if isinstance(exc.detail, dict) else exc.detail
        print(f"Evolution API error {exc.status}: {str(detail)[:300]}")
        return 1
    finally:
        await evo.aclose()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
