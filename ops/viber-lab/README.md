# viber-lab — тимчасовий доступ Claude Code до експериментів із Viber (фаза 2)

Дає користувачу `pdm_mess` (Claude Code, не в групі `docker`) запускати **лише** `viber-bridge/tools/lab.py`
всередині контейнера Viber від користувача `viber`. Інших команд Docker він не отримує.

Що це означає: через `lab.py` видно інтерфейс Viber акаунта магазину (дерево AT-SPI, скріншоти).
У `lab.py` немає команд, що вводять чи надсилають текст; тексти показуються лише як «форма»,
крім тих, що містять цифри з `/etc/viber-lab.reveal` (тестовий номер власника).

## Встановлення (власник, під `deploy`)

```bash
sudo install -m 755 -o root -g root /home/pdm_mess/VTW_CRM_Mass/ops/viber-lab/viber-lab /usr/local/sbin/viber-lab
echo 'XXXX' | sudo tee /etc/viber-lab.reveal >/dev/null     # останні 4 цифри свого номера
echo 'pdm_mess ALL=(root) NOPASSWD: /usr/local/sbin/viber-lab' | sudo tee /etc/sudoers.d/viber-lab >/dev/null
sudo chmod 440 /etc/sudoers.d/viber-lab && sudo visudo -cf /etc/sudoers.d/viber-lab
```

Скрипт копіюється в `/usr/local/sbin` (root), тож зміни в репозиторії на нього не впливають, доки
власник не перевстановить його. `lab.py` береться з робочої копії `pdm_mess` при кожному запуску.

## Відкликання (після фази 2)

```bash
sudo rm /etc/sudoers.d/viber-lab /usr/local/sbin/viber-lab /etc/viber-lab.reveal
rm -rf /home/pdm_mess/viber-lab-out    # скріншоти
```
