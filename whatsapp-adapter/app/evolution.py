"""Minimal Evolution API v2 client (routes as in evolution-api 2.3.7). Header `apikey` = global key."""

from __future__ import annotations

from typing import Any

import httpx

WEBHOOK_EVENTS = ["MESSAGES_UPSERT", "CONNECTION_UPDATE"]


class EvolutionError(RuntimeError):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(f"evolution http {status}")
        self.status = status
        self.detail = detail

    def not_on_whatsapp(self) -> bool:
        """sendText answers 400 with {..."message": [{"exists": false, ...}]} for numbers without WhatsApp."""
        return self.status == 400 and _has_exists_false(self.detail)


def _has_exists_false(value: Any) -> bool:
    if isinstance(value, dict):
        return value.get("exists") is False or any(_has_exists_false(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_exists_false(v) for v in value)
    return False


class EvolutionClient:
    def __init__(self, url: str, api_key: str, instance: str, client: httpx.AsyncClient | None = None) -> None:
        self.instance = instance
        self._client = client or httpx.AsyncClient(base_url=url, timeout=30, headers={"apikey": api_key})

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        resp = await self._client.request(method, path, **kw)
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        if resp.status_code >= 400:
            raise EvolutionError(resp.status_code, data)
        return data

    def _webhook(self, url: str, secret: str) -> dict[str, Any]:
        return {
            "enabled": True, "url": url, "byEvents": False, "base64": False,
            "headers": {"X-Webhook-Secret": secret}, "events": WEBHOOK_EVENTS,
        }

    async def instance_exists(self) -> bool:
        try:
            data = await self._req("GET", "/instance/fetchInstances", params={"instanceName": self.instance})
        except EvolutionError as exc:
            if exc.status == 404:  # 2.3.7 raises NotFoundException for an unknown instanceName
                return False
            raise
        items = data if isinstance(data, list) else [data]
        return any((i or {}).get("name") == self.instance or (i or {}).get("instanceName") == self.instance
                   for i in items if isinstance(i, dict))

    async def ensure_instance(self, webhook_url: str, webhook_secret: str) -> bool:
        """Create the Baileys instance if missing and (re)apply the webhook. True if it was created."""
        created = False
        if not await self.instance_exists():
            await self._req("POST", "/instance/create", json={
                "instanceName": self.instance,
                "integration": "WHATSAPP-BAILEYS",
                "qrcode": False,
                "groupsIgnore": True,       # private chats only
                "alwaysOnline": False,
                "readMessages": False,      # the operator reads in Odoo; do not mark as read
                "readStatus": False,
                "syncFullHistory": False,
                "rejectCall": False,
                "webhook": self._webhook(webhook_url, webhook_secret),
            })
            created = True
        await self._req("POST", f"/webhook/set/{self.instance}",
                        json={"webhook": self._webhook(webhook_url, webhook_secret)})
        return created

    async def connection_state(self) -> str:
        data = await self._req("GET", f"/instance/connectionState/{self.instance}")
        return str(((data or {}).get("instance") or {}).get("state") or "unknown")

    async def connect(self, number_digits: str | None = None) -> dict[str, Any]:
        params = {"number": number_digits} if number_digits else None
        return await self._req("GET", f"/instance/connect/{self.instance}", params=params)

    async def number_exists(self, number_digits: str) -> bool:
        data = await self._req("POST", f"/chat/whatsappNumbers/{self.instance}", json={"numbers": [number_digits]})
        first = data[0] if isinstance(data, list) and data else {}
        return bool(first.get("exists"))

    async def send_text(self, number_digits: str, text: str) -> str:
        data = await self._req("POST", f"/message/sendText/{self.instance}",
                               json={"number": number_digits, "text": text, "linkPreview": False})
        return str(((data or {}).get("key") or {}).get("id") or "")
