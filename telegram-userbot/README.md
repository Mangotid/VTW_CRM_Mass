# telegram-userbot

Міст між **особистим** корпоративним акаунтом Telegram (Telethon, userbot) і Odoo `crm_messenger`.
Потрібен, щоб оператор міг писати клієнту першим за номером телефону.
Telegram-**бот** для клієнтів, які пишуть самі, лишається в Odoo (`crm_messenger_telegram_bot`) — тут він не дублюється.

Контракт — `CLAUDE.md`; API, черги, ліміти й доставку в Odoo дає `common/bridge_core`.
Тут лише Telegram-специфіка: `app/gateway.py` (Telethon), `app/main.py` (логіка відправки та вхідних), `login.py`.

## Як працює

**Відправка** (`POST /send` → черга → по одному, пауза 4–10 с, ліміти):
1. Номер → користувач: спершу локальний кеш (`user_id` + `access_hash` у `bridge.sqlite3`),
   інакше `contacts.ImportContacts` (контакт з'являється в акаунті як «PDM +380...»). Нікого не знайдено → `not_found`.
2. `send_message(..., parse_mode=None)` — текст іде дослівно, без Markdown. `external_id` = `<user_id>:<message_id>`.

| Ситуація | Результат |
|----------|-----------|
| номер не в Telegram / приховує номер від не-контактів | `not_found` |
| номер недійсний, акаунт видалено | `not_found` (кеш очищується) |
| `FloodWaitError` | повідомлення повертається в чергу на `seconds + 5`, **уся** відправка ставиться на паузу; `/health` → `degraded`, `sender_paused` |
| Telegram просить повторити імпорт (`retry_contacts`) | повтор через 10 хв |
| `PeerFloodError` (акаунт обмежено за спам новим контактам) | `error` + пауза 30 хв — потрібна увага людини |
| користувач обмежив повідомлення / заблокував | `error` |
| застарілий кеш контакту, мережа | ще одна спроба через 30 с, далі `error` |
| сесію відкликано (`AuthKeyUnregistered` тощо) | `error`, `/health` → `logged_out`, `/send` → `503 telegram_logged_out` |

Флуд-очікування до `TG_FLOOD_SLEEP_THRESHOLD` (30 с) Telethon пересипляє сам.

**Вхідні**: лише приватні чати з людьми. Групи, канали, боти, власні повідомлення й службовий акаунт
Telegram `777000` (там приходять **коди входу**) ігноруються. Пропущене під час простою підтягується
(`catch_up`). Дедуплікація за `external_id` = `<user_id>:<message_id>`. Медіа поки не передається:
у тексті з'являється позначка `[вкладення: фото]` тощо.

### Розширення контракту (потребує погодження)

Telegram не завжди показує номер того, хто пише першим. Тому у вхідних:
- `phone` може бути `null`;
- додаються поля `user_id` (рядок, id користувача Telegram) і `username`.

Odoo зможе зіставляти партнера за `telegram_chat_id`.

Відповісти такому клієнту за номером не вийде. Для цього `/send` мав би приймати `user_id` — це окреме рішення.

### `/health`

`logged_out` — сесії немає або її відкликано. `degraded` — немає з'єднання з Telegram, відправка на паузі (flood)
або доставка в Odoo застрягла. `details`: `connected`, `authorized`, `sender_paused_until`.
Сервіс піднімає API одразу, навіть якщо Telegram недоступний. Підключення й перевірка сесії
(`updates.GetState`) виконуються у фоні кожні `TG_HEALTH_INTERVAL` секунд.

## Підготовка акаунта

1. Окрема корпоративна SIM. Встановити Telegram на телефон, увімкнути **двоетапну перевірку** (Налаштування → Конфіденційність).
2. На <https://my.telegram.org> → *API development tools* створити застосунок → `TG_API_ID`, `TG_API_HASH`.
   Це секрети: лише в `.env`.
3. Новий акаунт не варто одразу навантажувати: перші тижні — живе листування, малі ліміти.

## Запуск

```bash
cd telegram-userbot
cp .env.example .env && chmod 600 .env          # заповнити
docker compose build

# одноразовий вхід (сервіс має бути зупинений: сесія — SQLite-файл)
docker compose run --rm telegram-userbot python login.py
#   → код із застосунку Telegram на телефоні, потім пароль 2FA

docker compose up -d
docker compose logs -f telegram-userbot
```

`login.py` нічого не робить, якщо вхід уже виконано, і попереджає, якщо 2FA вимкнена.
У Telegram → Налаштування → Пристрої з'явиться «PDM Messenger Hub».

## Змінні оточення

Спільні — див. `common/README.md` (`SEND_ENABLED` для Telegram за замовчуванням `true`). Специфічні:

| Змінна | За замовч. | |
|--------|-----------|-|
| `TG_API_ID`, `TG_API_HASH` | — | з my.telegram.org, секрет |
| `TG_SESSION_PATH` | `/session/userbot` | Telethon додає `.session` |
| `TG_PHONE` | — | лише для `login.py` |
| `TG_DEVICE_MODEL` | `PDM Messenger Hub` | |
| `TG_CONTACT_LABEL` | `PDM` | префікс імені імпортованих контактів |
| `TG_HEALTH_INTERVAL` | 60 | с |
| `TG_FLOOD_SLEEP_THRESHOLD` | 30 | с |

## Перевірка

```bash
H="X-Bridge-Secret: $BRIDGE_SECRET"
curl -s -H "$H" http://10.77.0.1:8102/health | jq      # status: connected
curl -s -XPOST -H "$H" -H 'content-type: application/json' \
  -d '{"message_id": 900001, "phone": "+380XXXXXXXXX", "text": "Тест"}' http://10.77.0.1:8102/send
# написати на корпоративний номер з особистого Telegram → у логах incoming_queued, в Odoo — вхідне
```

## Розробка

```bash
python3 -m venv .venv && .venv/bin/pip install --no-deps -r requirements.lock && .venv/bin/pip install pytest==9.1.1
.venv/bin/python -m pytest -q
```

## Дані й безпека

| Том | Вміст |
|-----|-------|
| `tg_session` | файл сесії = **повний доступ до акаунта**. Права `600`, бекап лише в `/mnt/backup` (root, 600) |
| `tg_state` | черги мосту (тексти до доставки), кеш номер → користувач |

Контейнер працює з read-only кореневою ФС, від непривілейованого користувача.
Якщо сесію скомпрометовано: Telegram на телефоні → Пристрої → завершити «PDM Messenger Hub», потім знову `login.py`.
