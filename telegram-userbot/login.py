"""One-time interactive login for the Telegram userbot.

Run with the service STOPPED (the session file is SQLite and must not be shared):
    docker compose stop telegram-userbot
    docker compose run --rm telegram-userbot python login.py
    docker compose start telegram-userbot

Asks for the login code (sent to the Telegram app on the corporate phone) and the 2FA password.
Nothing is printed except masked account info; the code and password are never logged.
"""

from __future__ import annotations

import asyncio
import getpass
import os
import sys

from telethon import errors, functions

from app.config import TelegramApi
from app.gateway import build_client, protect_session_file
from bridge_core.logs import mask_phone


async def main() -> int:
    api = TelegramApi.from_env()
    client = build_client(api, catch_up=False)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Already logged in as id={me.id} phone={mask_phone('+' + (me.phone or ''))}. Nothing to do.")
            return 0

        phone = os.environ.get("TG_PHONE") or input("Phone number of the corporate account (+380...): ").strip()
        await client.send_code_request(phone)
        for _ in range(3):
            code = input("Login code from the Telegram app: ").strip()
            try:
                await client.sign_in(phone=phone, code=code)
                break
            except errors.PhoneCodeInvalidError:
                print("Invalid code, try again.")
            except errors.SessionPasswordNeededError:
                for _ in range(3):
                    try:
                        await client.sign_in(password=getpass.getpass("2FA password: "))
                        break
                    except errors.PasswordHashInvalidError:
                        print("Wrong 2FA password, try again.")
                else:
                    print("Too many wrong passwords.")
                    return 1
                break
        else:
            print("Too many invalid codes.")
            return 1

        me = await client.get_me()
        print(f"Logged in as id={me.id} phone={mask_phone('+' + (me.phone or ''))}")
        pwd = await client(functions.account.GetPasswordRequest())
        if not pwd.has_password:
            print("WARNING: two-step verification is OFF on this account. Enable it in Telegram settings.")
        return 0
    finally:
        await client.disconnect()
        protect_session_file(api)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
