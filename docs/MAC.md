# VagOn на Mac

## Установка

Нужен Python 3.10+ (`python3 --version`; если нет — `brew install python` или python.org).

```bash
git clone https://github.com/AlexanderSobin/VagOn.git
cd VagOn
python3 -m pip install -e ".[all]"
vagon --version
```
`[all]` ставит необязательные `bleak` (Bluetooth LE) и `pyserial` (USB и классический Bluetooth).
Для Wi‑Fi адаптера ничего, кроме Python, не нужно. Без установки можно запускать прямо из папки:
`python3 -m vagon …`.

## Подключение адаптера

| Тип ELM327 | Как подключить | Адрес для `-a` |
|---|---|---|
| Wi‑Fi | Подключите Mac к Wi‑Fi сети адаптера (WiFi_OBDII, V‑LINK, OBDII…) | `wifi` или `tcp://192.168.0.10:35000` |
| Bluetooth LE | Ничего не сопрягать в настройках macOS — VagOn найдёт сам | `ble` или `ble://ИМЯ` |
| Bluetooth classic (SPP) | Сопрягите в «Системные настройки → Bluetooth» (PIN обычно 1234/0000) | `serial:///dev/cu.ИМЯ` |
| USB | Просто вставьте | `serial:///dev/cu.usbserial-XXXX` |

Найти адаптер: `vagon adapters --ble` (порты и BLE‑устройства). Адрес по умолчанию можно задать один
раз: `export VAGON_ADAPTER=ble` (в `~/.zshrc`).

**BLE на macOS:** при первом запуске macOS спросит разрешение на Bluetooth для Терминала —
разрешите (Системные настройки → Конфиденциальность → Bluetooth).

**Wi‑Fi:** пока Mac подключён к адаптеру, интернета нет — это нормально, VagOn работает офлайн.

## Первый запуск

```bash
vagon -a sim scan          # сначала на симуляторе: всё то же самое, но без машины
vagon -a wifi info         # адаптер, напряжение, VIN
vagon -a wifi scan --dtc
vagon web --open           # веб‑интерфейс: http://127.0.0.1:8765
```

## Если что-то не так

* `адаптер не ответил (нет '>')` — неверный адрес/порт, адаптер не в машине, или зажигание выключено.
* `CAN ERROR` — нет связи с шиной: включите зажигание, переподключите адаптер.
* `адаптер не понимает команду …` / `нет ATFCSM1` — очень слабый клон ELM327. Нужен адаптер,
  который умеет `ATFCSH/ATFCSD/ATFCSM1` и `ATCRA` (обычно это v1.4b+ на PIC18F25K80 или чипы STN —
  OBDLink, vLinker).
* `BUFFER FULL` — увеличьте паузу между кадрами: `vagon --stmin 20 …`.
* Журнал всего обмена с адаптером — в `~/.vagon/logs/` (последние 30 сеансов). Его удобно приложить,
  если нужно разобраться в проблеме.

## Где лежат данные

`~/.vagon/` (можно переопределить `VAGON_HOME`):
```
backups/<VIN>/…json    резервные копии перед каждой записью
journal.jsonl          журнал записей
labels/user.json       выученные каналы адаптации
recipes/learned.json   выученные рецепты
dids/…                 какие идентификаторы есть у блоков (для обучения)
logs/session_*.log     обмен с адаптером
```
