# whatsapp-adapter

WhatsApp магазину (номер WhatsApp Business як **пов'язаний пристрій**) ↔ Odoo `crm_messenger`.
Транспорт — [Evolution API](https://github.com/evolution-foundation/evolution-api) `v2.3.7` (Baileys, без браузера).
Адаптер реалізує контракт із `CLAUDE.md` (`POST /send`, `GET /health` → Odoo `/messenger/bridge/whatsapp/...`)
на спільному ядрі `common/bridge_core`.

```
Odoo ─/send─▶ whatsapp-adapter ─▶ Evolution API (внутрішня мережа) ─▶ WhatsApp
Odoo ◀─incoming/status── adapter ◀─webhook messages.upsert (X-Webhook-Secret)── Evolution API
```

| Сервіс | Мережі | Порти |
|--------|--------|-------|
| `evolution-postgres` (16.15) | `wa_internal` (без виходу назовні) | — |
| `evolution-api` | `wa_internal`, `wa_egress` (до серверів WhatsApp) | — |
| `whatsapp-adapter` | `wa_internal`, `wa_egress` | `10.77.0.1:8103` (WireGuard) |

## Поведінка

- **Вихідні**: `chat/whatsappNumbers` → немає WhatsApp → `not_found`; інакше `message/sendText`. Помилка мережі
  до запиту — один повтор через 30 с; таймаут/5xx **під час** відправки → `error` («доставка невідома»), без повтору.
- **Вхідні**: лише приватні чати (`@s.whatsapp.net`; `@lid` — якщо Evolution знає номер). Групи, статуси, власні
  повідомлення, реакції, видалення/редагування — пропускаються (лічильники в `/health` → `details.skipped`).
  Старші за `WA_INCOMING_MAX_AGE_HOURS` (24) — пропускаються, щоб після прив'язки не залити Odoo історією.
  Медіа → `[вкладення: фото]` + підпис, без самого файлу.
- Evolution **не зберігає** повідомлення, чати й контакти (`DATABASE_SAVE_DATA_*=false`): запис — в Odoo.
- Evolution 2.3.7 виводить кожне вхідне повідомлення в stdout незалежно від `LOG_LEVEL`, тому його логи
  вимкнено (`logging: none`). Стан Evolution видно в `/health` адаптера (`details.state`).

## Запуск на hub

```bash
cd whatsapp-adapter
cp .env.example .env && chmod 600 .env
for v in EVOLUTION_API_KEY EVOLUTION_DB_PASSWORD WEBHOOK_SECRET BRIDGE_SECRET; do
  sed -i "s/^$v=.*/$v=$(openssl rand -hex 32)/" .env
done
docker compose build && docker compose up -d
docker compose run --rm whatsapp-adapter python login.py     # QR у терміналі (або --code: 8 символів)
```

На телефоні: WhatsApp Business → **Пов'язані пристрої** → **Прив'язати пристрій** → відсканувати QR із термінала
(оновлюється сам). Варіант `--code`: «Прив'язати за номером телефону» → ввести 8 символів (через Baileys спрацьовує
не завжди). Незавершене підключення (`connecting`) `login.py` спершу скидає. Чекає до 3 хв → `Linked. State: open.`

## Перевірка

```bash
H="X-Bridge-Secret: $(grep ^BRIDGE_SECRET .env | cut -d= -f2)"
curl -s -H "$H" http://10.77.0.1:8103/health      # status: connected, details.state: open
```

В Odoo: Налаштування → CRM Messenger → «Мости Telegram / WhatsApp» → Режим WhatsApp = «Міст (Evolution API)»,
адреса `http://10.77.0.1:8103`, секрет — `BRIDGE_SECRET` з цього `.env`.

## Дані й безпека

| Том | Вміст |
|-----|-------|
| `wa_pg` | інстанс і ключі сесії WhatsApp — доступ до акаунта |
| `wa_instances` | файли інстансу Evolution |
| `wa_state` | черги адаптера (тексти до доставки в Odoo) |

`EVOLUTION_API_KEY` дає повний контроль над акаунтом через Evolution; Evolution не публікує портів.
Процес адаптера — UID 10003 (не 1000, див. `CLAUDE.md`).

## Тести

```bash
python -m pytest -q      # conftest додає ../common у sys.path; Evolution замокано (httpx.MockTransport)
```
