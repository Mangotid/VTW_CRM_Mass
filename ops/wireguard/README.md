# WireGuard: hub ↔ Odoo

Приватний тунель між messenger hub і сервером Odoo. API мостів і webhook-и Odoo для мостів
ходять лише через нього.

| Вузол  | Адреса в тунелі | Роль |
|--------|-----------------|------|
| hub    | `10.77.0.1`     | слухає UDP `51820` (відкрито в `ufw` через `bootstrap.sh`) |
| Odoo   | `10.77.0.2`     | ініціює з'єднання (`Endpoint` + `PersistentKeepalive`), вхідний UDP не потрібен |

Інші адреси або порт: змінні `WG_NET_PREFIX`, `HUB_IP`, `ODOO_IP`, `WG_PORT` для `wg-setup.sh`.

Приватні ключі лежать у `/etc/wireguard/wg0.key` (права `0600`), а `wg0.conf` підвантажує їх через `PostUp`,
тож у самому конфігу немає секретів. Не надсилайте приватні ключі ні в чат, ні в git.

## Кроки

Скрипт є в репозиторії; на сервер Odoo достатньо скопіювати каталог `ops/wireguard/`.

1. **Обидва сервери: ключі.**
   ```bash
   sudo apt install wireguard-tools        # на hub уже стоїть після bootstrap.sh
   sudo ./ops/wireguard/wg-setup.sh keys   # виводить ПУБЛІЧНИЙ ключ
   ```
2. **(Рекомендовано) спільний PSK.** На hub:
   ```bash
   sudo ./ops/wireguard/wg-setup.sh psk
   ```
   Скопіюйте `/etc/wireguard/wg0.psk` на сервер Odoo в той самий шлях через SSH (`scp`, потім `chmod 600`, власник root).
   Файл секретний.
3. **Hub:**
   ```bash
   sudo PEER_PUBLIC_KEY='<публічний ключ Odoo>' ./ops/wireguard/wg-setup.sh hub
   ```
4. **Odoo:**
   ```bash
   sudo PEER_PUBLIC_KEY='<публічний ключ hub>' HUB_ENDPOINT='<публічна IP hub>' ./ops/wireguard/wg-setup.sh odoo
   ```
5. **Перевірка** на обох серверах:
   ```bash
   sudo ./ops/wireguard/wg-setup.sh status   # має бути "latest handshake: ... seconds ago"
   ping -c3 10.77.0.1                        # з Odoo
   ping -c3 10.77.0.2                        # з hub
   ```

`wg-setup.sh` ідемпотентний: ключі не перегенеровує, наявний `wg0.conf` не перезаписує
(якщо він відрізняється, показує diff), вмикає `wg-quick@wg0`. Також додає drop-in
`docker.service.d/10-after-wireguard.conf`, щоб Docker стартував після `wg0`. Docker при цьому
не перезапускається.

## Сервер Odoo: що змінити в його compose (вручну, після погодження)

Мости шлють `POST /messenger/bridge/<channel>/...` на `ODOO_URL=http://10.77.0.2:8069`.
Для цього контейнер `web` має бути доступний на адресі тунелю. Додайте в сервіс `web` компоуза Odoo:

```yaml
    ports:
      - "10.77.0.2:8069:8069"
```

- Порт опубліковано лише на `10.77.0.2`, тож з інтернету він недоступний (Docker обходить `ufw`, тому прив'язка до IP обов'язкова).
- Запити йдуть повз Caddy. Якщо в Odoo кілька баз, потрібен `dbfilter` або `db_name`, інакше контролер не знайде базу (розберемо у фазі 5).
- Після зміни: `docker compose up -d web` (короткий перезапуск Odoo).

Альтернатива без зміни compose — слати мости через Caddy на `https://portdemaison.com/messenger/bridge/...`.
Тоді webhook-и стають публічними (захищені лише секретом), тому цей варіант не рекомендую.

## Відкат

```bash
sudo systemctl disable --now wg-quick@wg0
# конфіг і ключі лишаються в /etc/wireguard; ufw-правило для UDP прибирає власник вручну
```
