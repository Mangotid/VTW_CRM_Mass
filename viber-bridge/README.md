# viber-bridge

Міст між особистим акаунтом Viber магазину (Viber Desktop у Docker) і Odoo `crm_messenger`.
Реалізує контракт із `CLAUDE.md`: `POST /send`, `GET /health` → Odoo `/messenger/bridge/viber/{incoming,status}`.
API, черги, ліміти й доставка в Odoo — спільне ядро `common/bridge_core` (див. `common/README.md`);
тут лише Viber-специфіка: `app/viber_ui.py` (UI: AT-SPI, OCR), `app/notify.py` (сервер сповіщень), `app/workers.py` (відправка, вхідні).

## Стан

`viber.db` у Viber Desktop 27.x зашифрована, а дерево AT-SPI не містить чатів і повідомлень, тому міст
працює через інтерфейс: сповіщення, AT-SPI і OCR. Дослідження — `docs/viber-ui-research.md`.

| Частина | Стан |
|---------|------|
| Образ, QR-вхід, noVNC | працює на hub |
| API `/send`, `/health`, черги, ліміти, доставка в Odoo | готово, тести (`common/bridge_core`) |
| Вхідні зі сповіщень + номер відправника через OCR | написано, тести з фейковим UI; **перевірити на живому** |
| Відправка з перевіркою адресата | написано, тести з фейковим UI; **перевірити на живому**, до того `SEND_ENABLED=false` |
| Вкладення | не підтримуються (`422`); вхідні медіа → `[вкладення: фото]` без файлу й підпису |

## Як працює

Усі дії з вікном Viber — під одним замком, по одній. Після кожної дії міст повертає Viber у **нейтральний стан**:
відкритий чат номера, якого немає у Viber (`VIBER_NEUTRAL_PHONE`), — без поля введення. Так жоден чат клієнта
не лишається відкритим (Viber не сповіщає про повідомлення у відкритому чаті).

**Відправка** (одна спроба = кроки нижче; повтор лише якщо нічого не вставлено):

1. Розгорнути вікно; нейтральний стан — перевірено: поля введення немає, кнопка `Viber Out`, у заголовку нейтральний номер.
2. `xdg-open viber://chat?number=...`. Немає поля введення + `Viber Out` + номер клієнта в заголовку → `not_found`.
3. Поле введення з'явилося → навести мишу на рядок статусу в заголовку → OCR цифр → номер **має збігтися** з цільовим.
   Не збігся або не прочитався → нічого не вставляється.
4. `xclip` → Ctrl+V → текст у полі (AT-SPI) має збігтися з повідомленням (`PASTE_CHECK`), інакше поле очищується.
5. Enter → поле порожнє → `sent`. Не спорожніло → `error` («доставка невідома»), без повторної відправки.

**Вхідні**: міст сам є сервером `org.freedesktop.Notifications`; Viber стартує лише після нього (інакше не шле
сповіщень). Сповіщення (ім'я + повний текст) → міст «клацає» його (`ActionInvoked`) → Viber відкриває чат
відправника → наведення на статус → OCR номера → у чергу для Odoo → нейтральний стан. Без номера (група,
прихований номер, помилка OCR) повідомлення в Odoo не йде: `incoming_unresolved` у `/health` і скріншот у `/debug`.

- Ідемпотентність `/send` за `message_id`; вхідні дедуплікуються за `external_id` (`vn:<hash>`).
- Повідомлення, що були в `sending` під час падіння, → `error`, без повторної відправки.
- Денні ліміти, черга в Odoo з backoff, JSON-логи без текстів — як у спільному ядрі.

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
не працює сервер сповіщень або доставка в Odoo застрягла довше `CALLBACK_STALE_AFTER`; інакше `connected`.
`reasons` пояснює причину; `daily_limit_reached` і `sending_disabled` статус не погіршують.
Viber-специфічні поля — у `details`: `viber_running`, `notify_server`, `incoming_pending`, `incoming_unresolved`.

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
3. Перевірити: `GET /health` → `status` не `logged_out`, `details.notify_server: true`.

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
