# ops — підготовка messenger hub

Скрипти запускає власник сервера. Claude Code їх не запускає.

| Файл | Призначення |
|------|-------------|
| `bootstrap.sh` | первинне налаштування hub (Ubuntu 24.04) |
| `bootstrap.conf.example` | конфіг для `bootstrap.sh` → копія `bootstrap.conf` (git-ignored) |
| `wireguard/` | тунель hub ↔ Odoo: `wg-setup.sh`, шаблони, інструкція |

## Що робить `bootstrap.sh`

Кроки йдуть по порядку; кожен спершу перевіряє поточний стан, тож повторний запуск безпечний.

| Крок | Дії |
|------|-----|
| `preflight` | root, Ubuntu 24.04 x86_64, валідність конфігу й SSH-ключів, `SSH_PORT` справді слухається |
| `packages` | `apt update` + `full-upgrade`, базові пакети (ufw, wireguard-tools, unattended-upgrades, jq, sqlite3, rsync) |
| `timezone` | `Europe/Kyiv`, NTP |
| `swap` | `/swapfile` 4 ГБ + `fstab`, `vm.swappiness=10`. Якщо вже є інший swap — нічого не робить |
| `user` | користувач `deploy` без пароля, ключі в `authorized_keys`, опційно група `sudo` |
| `ssh` | drop-in `sshd_config.d/10-pdm-hardening.conf`: вхід лише за ключем, root — `no`. Відмовляється, якщо в `deploy` немає ключів; перевіряє `sshd -t` і відкочує зміну при помилці |
| `firewall` | `ufw`: вхідні заборонено, крім SSH (`limit`) і WireGuard UDP |
| `docker` | офіційний репозиторій (перевірка відбитка GPG), зафіксовані версії + `apt-mark hold`, ротація логів, `live-restore`, старт після `wg0`, `deploy` у групі `docker` |
| `unattended` | щоденні оновлення безпеки, без автоперезавантаження (`AUTO_REBOOT=no`) |
| `backup` | HDD за UUID → `/mnt/backup` (`nofail`, права `700 root`). **Диск не форматує**; якщо `BACKUP_UUID` порожній — лише показує `lsblk -f` |

Скрипт **не** змінює порт SSH, не форматує диски і не видаляє пакети. Якщо стоїть конфліктний
`docker.io`, він зупиняється. Лог пишеться в `/var/log/pdm-bootstrap.log` (права `600`).

## Порядок дій

```bash
# 0. На своєму ПК: ключ, якщо ще немає
ssh-keygen -t ed25519 -C "pdm-hub"

# 1. На сервері (поки що під root або користувачем із sudo)
git clone <repo> /opt/pdm-hub && cd /opt/pdm-hub
cp ops/bootstrap.conf.example ops/bootstrap.conf && nano ops/bootstrap.conf
sudo ./ops/bootstrap.sh preflight packages timezone swap user
#    → у НОВОМУ терміналі перевірити: ssh deploy@<host>
sudo passwd deploy                       # якщо DEPLOY_SUDO=yes
sudo ./ops/bootstrap.sh ssh firewall     # поточну сесію НЕ закривати, доки не перевірите вхід ще раз
sudo ./ops/bootstrap.sh docker unattended

# 2. HDD під бекапи
lsblk -f                                 # знайти розділ HDD та його UUID
#   якщо файлової системи немає — створити її вручну (знищує дані на розділі!):
#   sudo mkfs.ext4 -L pdm-backup /dev/sdX1
#   вписати BACKUP_UUID у bootstrap.conf
sudo ./ops/bootstrap.sh backup

# 3. WireGuard — див. ops/wireguard/README.md

# 4. Якщо потрібно: sudo reboot   (скрипт попередить, якщо оновлення ядра цього вимагають)
```

Можна запускати й усе одразу (`sudo ./ops/bootstrap.sh`), але покроково безпечніше через SSH.

## Перевірка

```bash
sudo sshd -T | grep -E 'passwordauthentication|permitrootlogin|pubkeyauthentication'
sudo ufw status verbose
swapon --show && cat /proc/sys/vm/swappiness
timedatectl | grep 'Time zone'
docker version --format '{{.Server.Version}}' && docker compose version
apt-mark showhold
systemctl cat docker | grep -A2 10-after-wireguard
findmnt /mnt/backup && stat -c '%a %U' /mnt/backup
systemctl is-enabled unattended-upgrades
```

Очікується: `passwordauthentication no`, `permitrootlogin no`; у `ufw` лише SSH і `51820/udp`;
swap 4G, `swappiness` 10; `Europe/Kyiv`; Docker `29.8.2`, compose `5.5.1`, 5 пакетів на hold;
`/mnt/backup` змонтовано з правами `700 root`.

## Відомі нюанси

- **Docker і ufw.** Опубліковані порти контейнерів обходять `ufw`. Тому всі сервіси публікують
  порти лише на WireGuard-адресі (`10.77.0.1:...`), а не на `0.0.0.0`. Це перевіряється в ревʼю compose-файлів.
- **Docker стартує після `wg0`.** Інакше прив'язка до `10.77.0.1` падає з `cannot assign requested address`.
- **Оновлення Docker** — лише свідомо і вручну: `sudo apt-mark unhold docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin`,
  `sudo apt-get install docker-ce=<ver> docker-ce-cli=<ver> ...`, потім оновити версії в `bootstrap.conf`
  і запустити `sudo ./ops/bootstrap.sh docker` (поверне hold). Скрипт сам наявну версію не змінює, лише попереджає.
- `deploy` у групі `docker` має фактично root-доступ. Для Claude Code буде окремий обмежений користувач (фаза 7).
