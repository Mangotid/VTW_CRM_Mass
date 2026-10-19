# common/bridge_core

Спільне ядро всіх мостів (Viber, Telegram, WhatsApp). Реалізує контракт із `CLAUDE.md`,
щоб мости відрізнялися лише транспортом.

| Модуль        | Що містить |
|---------------|------------|
| `models.py`   | Pydantic-моделі контракту (`SendRequest`, `IncomingPayload`, `StatusPayload`, `HealthResponse`), нормалізація E.164 |
| `settings.py` | `CoreSettings` і `core_env()` — спільні змінні оточення |
| `store.py`    | SQLite у томі: outbox, черга колбеків в Odoo, дедуплікація вхідних, денні лічильники, kv |
| `workers.py`  | `SenderLoop` (послідовно, пауза, ліміти, `pause()` для flood wait), `OdooDelivery` (FIFO, backoff) |
| `api.py`      | `BridgeBase` + `create_app()`: `POST /send`, `GET /health`, перевірка `X-Bridge-Secret` |
| `logs.py`     | JSON-логи, `mask_phone()` |

## Як зробити міст

```python
class MyBridge(BridgeBase):
    async def handle_outgoing(self, row, today):   # одна спроба; сам викликає mark_sending / sent / failed / store.defer
        ...
    def channel_health(self) -> ChannelHealth: ...  # logged_in, reasons, details, last_activity
    def send_unavailable(self) -> str | None: ...   # 503 для /send, якщо канал не готовий
    def extra_tasks(self): return {"watcher": self.watcher.run}  # ім'я → фабрика циклу; після падіння перезапускається
    async def startup(self) / shutdown(self): ...

app = create_app(factory=build, title="PDM ... bridge")
```

## Спільні змінні оточення

| Змінна | За замовчуванням | |
|--------|------------------|-|
| `BRIDGE_SECRET` | — | ≥ 32 символи, той самий у налаштуваннях каналу в Odoo |
| `ODOO_URL` | — | напр. `http://10.77.0.2:8069` |
| `ODOO_TIMEOUT` | 15 | с |
| `DATA_DIR` | `/data` | том із `bridge.sqlite3` |
| `TZ` | `Europe/Kyiv` | межа доби для лімітів |
| `SEND_ENABLED` | залежить від каналу | `false` → `/send` = `503 sending_disabled` |
| `SEND_DELAY_MIN` / `SEND_DELAY_MAX` | 4 / 10 | с, випадкова пауза між повідомленнями |
| `DAILY_LIMIT_TOTAL` | 150 | |
| `DAILY_LIMIT_NEW_CONTACTS` | 20 | |
| `CALLBACK_STALE_AFTER` | 600 | с; старіший колбек → `degraded` |

## Тести

```bash
cd common && python -m pytest -q
```
