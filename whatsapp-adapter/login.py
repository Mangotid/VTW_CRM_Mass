"""Link the WhatsApp Business number to Evolution API with a pairing code (no QR needed).

    docker compose run --rm whatsapp-adapter python login.py

On the phone: WhatsApp Business -> Linked devices -> Link a device -> "Link with phone number instead",
then type the 8-character code printed here. Nothing secret is printed.
"""

from __future__ import annotations

import asyncio
import os
import re
import sys

from app.config import EvolutionApi
from app.evolution import EvolutionClient, EvolutionError


async def main() -> int:
    api = EvolutionApi.from_env()
    evo = EvolutionClient(api.url, api.api_key, api.instance)
    try:
        created = await evo.ensure_instance(api.webhook_url, api.webhook_secret)
        print(f"Instance '{api.instance}': {'created' if created else 'exists'}, webhook set.")
        state = await evo.connection_state()
        if state == "open":
            print("Already linked (state: open). Nothing to do.")
            return 0
        raw = os.environ.get("WA_PHONE") or input("WhatsApp Business number (+380...): ")
        digits = re.sub(r"\D", "", raw)
        if len(digits) < 10:
            print("Invalid number.")
            return 1
        data = await evo.connect(digits)
        code = (data or {}).get("pairingCode")
        if not code:
            print(f"No pairing code returned (state: {state}). Try again in a minute.")
            return 1
        print(f"\nPairing code:  {code[:4]}-{code[4:]}\n")
        print("Phone: WhatsApp Business -> Linked devices -> Link a device -> Link with phone number instead.")
        for _ in range(60):
            await asyncio.sleep(3)
            if await evo.connection_state() == "open":
                print("Linked. State: open.")
                return 0
        print("Not linked within 3 minutes. Run login.py again for a new code.")
        return 1
    except EvolutionError as exc:
        detail = exc.detail.get("response", exc.detail) if isinstance(exc.detail, dict) else exc.detail
        print(f"Evolution API error {exc.status}: {str(detail)[:300]}")
        return 1
    finally:
        await evo.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
