from __future__ import annotations

import asyncio
import json
import time

import httpx
from fastapi.testclient import TestClient

from bridge_core.api import create_app
from bridge_core.store import EnqueueResult
from bridge_core.workers import local_today

from app.evolution import EvolutionClient, EvolutionError
from app.main import WhatsAppBridge, add_webhook_route
from app.mapping import parse_upsert, phone_from_jid
from tests.conftest import SECRET, WEBHOOK_SECRET

PHONE = "+380671234567"
NOW = 1_760_900_000.0


def upsert(**kw):
    data = {
        "key": {"remoteJid": "380671234567@s.whatsapp.net", "fromMe": False, "id": "3EB0ABC"},
        "pushName": "Олена", "message": {"conversation": "Добрий день"},
        "messageType": "conversation", "messageTimestamp": int(NOW) - 5,
    }
    data.update(kw.pop("data", {}))
    return {"event": "messages.upsert", "instance": "pdm", "data": data, **kw}


# -- mapping ---------------------------------------------------------------------

def test_parse_text_message():
    p = parse_upsert(upsert(), NOW, 3600)
    assert p.reason == "ok"
    assert p.payload["phone"] == PHONE and p.payload["text"] == "Добрий день"
    assert p.payload["external_id"] == "wa:3EB0ABC" and p.payload["name"] == "Олена"


def test_parse_skips_groups_own_old_and_reactions():
    assert parse_upsert(upsert(data={"key": {"remoteJid": "1203@g.us", "fromMe": False, "id": "x"}}), NOW, 3600).reason == "not_private"
    assert parse_upsert(upsert(data={"key": {"remoteJid": "380671234567@s.whatsapp.net", "fromMe": True, "id": "x"}}), NOW, 3600).reason == "from_me"
    assert parse_upsert(upsert(data={"messageTimestamp": int(NOW) - 7200}), NOW, 3600).reason == "too_old"
    assert parse_upsert(upsert(data={"messageType": "reactionMessage", "message": {}}), NOW, 3600).reason == "ignored_type"
    assert parse_upsert({"event": "contacts.update"}, NOW, 3600).reason == "not_upsert"


def test_parse_lid_without_phone_and_with_alt():
    lid = {"remoteJid": "1234567890123@lid", "fromMe": False, "id": "x"}
    assert parse_upsert(upsert(data={"key": lid}), NOW, 3600).reason == "no_phone"
    with_alt = {**lid, "remoteJidAlt": "380671234567@s.whatsapp.net"}
    assert parse_upsert(upsert(data={"key": with_alt}), NOW, 3600).payload["phone"] == PHONE


def test_parse_media_with_caption():
    p = parse_upsert(upsert(data={"messageType": "imageMessage", "message": {"imageMessage": {"caption": "Цей диван"}}}), NOW, 3600)
    assert p.payload["text"] == "Цей диван\n[вкладення: фото]"
    p = parse_upsert(upsert(data={"messageType": "audioMessage", "message": {"audioMessage": {}}}), NOW, 3600)
    assert p.payload["text"] == "[вкладення: аудіо]"


def test_phone_from_jid():
    assert phone_from_jid("380671234567:12@s.whatsapp.net") == PHONE
    assert phone_from_jid("1203@g.us") is None


# -- outgoing via a mocked Evolution -------------------------------------------------

class FakeEvolution:
    def __init__(self) -> None:
        self.exists = True
        self.send_status = 201
        self.send_exc: Exception | None = None
        self.sent: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["apikey"] == "k"
        path = request.url.path
        if path == "/chat/whatsappNumbers/pdm":
            num = json.loads(request.content)["numbers"][0]
            return httpx.Response(200, json=[{"exists": self.exists, "jid": f"{num}@s.whatsapp.net", "number": num}])
        if path == "/message/sendText/pdm":
            if self.send_exc:
                raise self.send_exc
            body = json.loads(request.content)
            self.sent.append(body)
            if self.send_status >= 400:
                return httpx.Response(self.send_status, json={"status": self.send_status, "error": "x"})
            return httpx.Response(201, json={"key": {"id": "3EB0SENT", "fromMe": True}})
        if path == "/instance/connectionState/pdm":
            return httpx.Response(200, json={"instance": {"instanceName": "pdm", "state": "open"}})
        if path == "/instance/fetchInstances":
            return httpx.Response(200, json=[{"name": "pdm"}])
        if path == "/webhook/set/pdm":
            hook = json.loads(request.content)["webhook"]
            assert hook["headers"]["X-Webhook-Secret"] == WEBHOOK_SECRET
            return httpx.Response(201, json={"webhook": hook})
        return httpx.Response(404)


def _bridge(settings, store, fake):
    client = httpx.AsyncClient(base_url="http://evo.test", headers={"apikey": "k"},
                               transport=httpx.MockTransport(fake.handler))
    return WhatsAppBridge(settings, store, EvolutionClient("http://evo.test", "k", "pdm", client))


def _enqueue(store, settings, mid=1):
    res, _ = store.enqueue_outgoing(mid, PHONE, "Привіт", local_today(settings), 100, 100)
    assert res == EnqueueResult.CREATED
    return store.next_queued()


def _run(bridge, row, settings):
    asyncio.run(bridge.handle_outgoing(row, local_today(settings)))


def test_send_ok(settings, store):
    fake = FakeEvolution()
    b = _bridge(settings, store, fake)
    _run(b, _enqueue(store, settings), settings)
    assert fake.sent == [{"number": "380671234567", "text": "Привіт", "linkPreview": False}]
    out = store.get_outgoing(1)
    assert out.state == "sent" and out.external_id == "wa:3EB0SENT"


def test_not_on_whatsapp(settings, store):
    fake = FakeEvolution()
    fake.exists = False
    b = _bridge(settings, store, fake)
    _run(b, _enqueue(store, settings), settings)
    assert fake.sent == [] and store.get_outgoing(1).state == "not_found"


def test_timeout_during_send_is_not_resent(settings, store):
    fake = FakeEvolution()
    fake.send_exc = httpx.ReadTimeout("slow")
    b = _bridge(settings, store, fake)
    _run(b, _enqueue(store, settings), settings)
    out = store.get_outgoing(1)
    assert out.state == "error" and "доставка невідома" in out.error
    assert store.next_queued(now=time.time() + 1e6) is None


def test_evolution_error_exists_false_is_not_found():
    exc = EvolutionError(400, {"status": 400, "response": {"message": [{"exists": False, "jid": "x", "number": "1"}]}})
    assert exc.not_on_whatsapp()
    assert not EvolutionError(400, {"response": {"message": ["Text is required"]}}).not_on_whatsapp()
    assert not EvolutionError(500, {"exists": False}).not_on_whatsapp()


def test_missing_instance_is_created(settings, store):
    fake = FakeEvolution()
    created = []
    base = fake.handler

    def handler(request):
        if request.url.path == "/instance/fetchInstances" and not created:
            return httpx.Response(404, json={"status": 404, "error": "Not Found",
                                             "response": {"message": ['Instance "pdm" not found']}})
        if request.url.path == "/instance/create":
            body = json.loads(request.content)
            assert body["integration"] == "WHATSAPP-BAILEYS" and body["groupsIgnore"] is True
            assert body["webhook"]["headers"]["X-Webhook-Secret"] == WEBHOOK_SECRET
            created.append(body["instanceName"])
            return httpx.Response(201, json={"instance": {"instanceName": "pdm"}})
        return base(request)

    fake.handler = handler
    b = _bridge(settings, store, fake)
    asyncio.run(b.check_evolution())
    assert created == ["pdm"] and b.webhook_ready and b.state == "open"


def test_check_evolution_sets_webhook_and_state(settings, store):
    b = _bridge(settings, store, FakeEvolution())
    asyncio.run(b.check_evolution())
    assert b.webhook_ready and b.evolution_reachable and b.state == "open"
    assert b.send_unavailable() is None


# -- webhook route + health ----------------------------------------------------------

def test_webhook_route(settings, store):
    b = _bridge(settings, store, FakeEvolution())
    app = create_app(bridge=b, start_workers=False)
    add_webhook_route(app)
    with TestClient(app) as c:
        assert c.post("/evolution/webhook", json=upsert()).status_code == 401
        h = {"X-Webhook-Secret": WEBHOOK_SECRET}
        event = upsert(data={"messageTimestamp": int(time.time())})
        assert c.post("/evolution/webhook", json=event, headers=h).json()["result"] == "ok"
        assert c.post("/evolution/webhook", json=event, headers=h).json()["result"] == "ok"  # duplicate
        group = upsert(data={"key": {"remoteJid": "1@g.us", "fromMe": False, "id": "g"}})
        assert c.post("/evolution/webhook", json=group, headers=h).json()["result"] == "not_private"
        conn = {"event": "connection.update", "data": {"state": "open"}}
        assert c.post("/evolution/webhook", json=conn, headers=h).json()["result"] == "connection"
        health = c.get("/health", headers={"X-Bridge-Secret": SECRET}).json()
        assert store.callbacks_stats()[0] == 1  # dedup by external_id
    assert health["details"]["skipped"] == {"not_private": 1}
    assert b.state == "open"


def test_logged_out_refuses_send(settings, store):
    b = _bridge(settings, store, FakeEvolution())
    b.state = "close"
    with TestClient(create_app(bridge=b, start_workers=False)) as c:
        r = c.post("/send", json={"message_id": 1, "phone": PHONE, "text": "x"}, headers={"X-Bridge-Secret": SECRET})
        assert r.status_code == 503 and r.json()["detail"] == "whatsapp_logged_out"
        assert c.get("/health", headers={"X-Bridge-Secret": SECRET}).json()["status"] == "logged_out"
