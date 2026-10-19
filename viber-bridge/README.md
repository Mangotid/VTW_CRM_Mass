# viber-bridge

Міст між особистим акаунтом Viber магазину (Viber Desktop у Docker) і Odoo `crm_messenger`.
Реалізує контракт із `CLAUDE.md`: `POST /send`, `GET /health` → Odoo `/messenger/bridge/viber/{incoming,status}`.
API, черги, ліміти й доставка в Odoo — спільне ядро `common/bridge_core` (див. `common/README.md`);
тут лише Viber-специфіка: `app/viber_ui.py`, `app/viber_db.py`, `app/workers.py` (спроба відправки, Watcher).

## Стан

| Частина                                   | Стан |
|-------------------------------------------|------|
| Образ: Xvfb + fluxbox + x11vnc + noVNC + Viber | написано, **ще не зібрано** (CDN Viber недоступний із середовища розробки) |
| API `/send`, `/health`, автентифікація, ідемпотентність | готово, покрито тестами |
| Черга відправки, паузи, денні ліміти, повтор один раз, скріншоти | готово, покрито тестами (з фейковим UI) |
| Локальна черга доставки в Odoo (FIFO, backoff) | готово, покрито тестами |
| Читання `viber.db` (вхідні, підтвердження відправки) | **чекає вивчення схеми** → `docs/viber-db-schema.md` |
| Перевірка, що відкрито чат саме потрібного клієнта | **не реалізовано** — відправка заблокована |
| Вкладення                                 | не підтримуються (`422 attachments_not_supported`) |

Поки не закриті два пункти «чекає» / «не реалізовано», `SEND_ENABLED=false` і `/send` відповідає `503 sending_disabled`.

### Чому відправка заблокована

Deep link `viber://chat?number=...` може не відкрити новий чат (номер не у Viber, діалог, помилка).
Тоді Ctrl+V + Enter надішле текст у **попередній відкритий чат — іншому клієнту**.
`X11ViberUi.verify_target()` має довести, що активний чат належить цьому номеру; до того, як спосіб
перевірки буде знайдено на живому Viber, вона завжди падає, і текст не вставляється.

## Як працює

```
Odoo ─POST /send─▶ outbox (SQLite /data) ─▶ Sender (послідовно, пауза 4–10 с, ліміти)
                                            ├─ baseline = останній event у viber.db
                                            ├─ xdg-open viber://chat?number=%2B380...
                                            ├─ verify_target(phone)
                                            ├─ xclip → Ctrl+V → Enter, очищення буфера
                                            └─ чекає вихідну подію в viber.db (таймаут)
                                               успіх → sent; ні → скріншот, ще 1 спроба → error
viber.db (mode=ro) ─▶ Watcher (за курсором event id) ─▶ callbacks (SQLite) ─▶ Odoo
                                                         статуси й вхідні, FIFO, повтор з backoff
```

- Ідемпотентність: повторний `/send` з тим самим `message_id` → `202 {"accepted": true, "duplicate": true, "state": ...}`,
  фінальний статус надсилається в Odoo ще раз. Той самий `message_id` з іншим номером → `409`.
- Вхідні дедуплікуються за `external_id` (таблиця `seen_incoming`).
- Після першого старту Watcher ставить курсор на останню подію — історію в Odoo не заливає.
- Повідомлення, що були в стані `sending` під час падіння, позначаються `error` (доставка невідома) і **не** відправляються повторно.
- Текст повідомлення стирається з `outbox` після фінального стану; колбеки видаляються після прийняття Odoo.
- Денні ліміти рахуються за київським часом. При перевищенні `/send` → `429 daily_limit` або `429 daily_new_contact_limit`.
  «Новий контакт» — номер, якому ми ще не писали і від якого не було вхідних (за локальною базою мосту).
- Odoo недоступний або відповідає не `2xx` → колбек лишається в черзі, повтор з backoff (мережа/5xx: до 5 хв,
  4xx: до 1 год). Дані не відкидаються.
- Логи — JSON у stdout, лише ID, маскований номер і метадані. Вивід самого Viber у логи не потрапляє.

### Відповіді `/send`

| Код | `detail`                            | Що робити Odoo |
|-----|-------------------------------------|----------------|
| 202 | —                                   | чекати `/status` |
| 401 | `invalid bridge secret`             | помилка конфігурації |
| 409 | `message_id_reused_with_other_phone`| помилка, не повторювати |
| 422 | валідація / `attachments_not_supported` | помилка, не повторювати |
| 429 | `daily_limit`, `daily_new_contact_limit` | повторити пізніше (наступного дня) |
| 503 | `sending_disabled`, `viber_logged_out` | повторити пізніше |

### `/health`

`status`: `logged_out` — немає `viber.db` (акаунт не залогінений); `degraded` — Viber не запущений,
Watcher не в стані `ok` або доставка в Odoo застрягла довше `CALLBACK_STALE_AFTER`; інакше `connected`.
`reasons` пояснює причину; `daily_limit_reached` і `sending_disabled` статус не погіршують.
Viber-специфічні поля — у `details`: `viber_running`, `viber_db_found`, `watcher`.

## Змінні оточення

Див. `.env.example` (усі з коментарями). Обов'язкові: `BRIDGE_SECRET` (≥ 32 символи), `ODOO_URL`,
`VNC_PASSWORD`, `WG_BIND_IP`, `VIBER_DEB_SHA256`.

## Запуск на hub

```bash
cd viber-bridge
cp .env.example .env && chmod 600 .env       # заповнити секрети
curl -fsSL https://download.cdn.viber.com/cdn/desktop/Linux/viber.deb | sha256sum   # → VIBER_DEB_SHA256
docker compose build
docker compose up -d
docker compose logs -f viber-bridge
```

Порти публікуються лише на `WG_BIND_IP`, тому `wg0` має бути піднятий до старту Docker (фаза 1).

### Перший вхід (QR)

1. З машини в WireGuard-мережі відкрити `http://10.77.0.1:6080/vnc.html`, пароль — `VNC_PASSWORD`.
2. У Viber Desktop з'явиться QR-код — відсканувати телефоном із корпоративною SIM (Viber → QR-сканер).
3. Перевірити: `GET /health` → `viber_db_found: true`.

### Що перевірити на першій збірці

- Viber стартує під Xvfb. Якщо падає через sandbox QtWebEngine — розкоментувати `QTWEBENGINE_DISABLE_SANDBOX=1`.
- `WM_CLASS` вікна Viber (`docker compose exec viber-bridge xprop -root _NET_ACTIVE_WINDOW`, потім `xprop -id <id> WM_CLASS`)
  → `VIBER_WINDOW_CLASS`.
- Чи існує `/usr/share/applications/viber.desktop` і чи `xdg-open viber://...` відкриває чат.
- Кирилиця й емодзі вставляються через буфер обміну без спотворень.

## Перевірка

```bash
H="X-Bridge-Secret: $BRIDGE_SECRET"
curl -s -H "$H" http://10.77.0.1:8101/health | jq
curl -s -XPOST -H "$H" -H 'content-type: application/json' \
  -d '{"message_id": 1, "phone": "+380XXXXXXXXX", "text": "Тест"}' http://10.77.0.1:8101/send
```

## Розробка й тести

```bash
python3 -m venv .venv && .venv/bin/pip install --no-deps -r requirements.lock && .venv/bin/pip install pytest==9.1.1
.venv/bin/python -m pytest -q                       # тести Viber (conftest додає ../common у sys.path)
(cd ../common && ../viber-bridge/.venv/bin/python -m pytest -q)   # тести ядра
```

Образ збирається з кореня репозиторію (`context: ..` у compose), бо копіює `common/bridge_core`.

Залежності: прямі — `requirements.txt`, повний зафіксований набір — `requirements.lock`
(образ ставить саме його з `--no-deps`). Під час оновлення фіксувати лише релізи, старші за два тижні.

## Дані й безпека

Процеси в контейнері працюють під UID `10001` (не 1000: користувач хоста з тим самим UID мав би доступ до сесії
через `/proc/<pid>/root`). Томи, створені старішим образом (UID 1000), треба один раз переписати:

```bash
docker compose down
docker run --rm -v pdm-viber_viber_profile:/a -v pdm-viber_viber_state:/b -v pdm-viber_viber_debug:/c \
  ubuntu:noble-20260911 chown -R 10001:10001 /a /b /c
```

| Том             | Вміст | Чутливість |
|-----------------|-------|------------|
| `viber_profile` | сесія акаунта Viber, `viber.db` | доступ до акаунта — лише бекап (root, 600) |
| `viber_state`   | черги мосту (тексти до доставки) | персональні дані клієнтів |
| `viber_debug`   | скріншоти помилок (можуть показувати чати) | персональні дані, автовидалення через `DEBUG_RETENTION_DAYS` |
