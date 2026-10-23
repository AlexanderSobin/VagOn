# VagOn на iPhone

iOS‑приложения не могут работать с **классическим** Bluetooth (SPP), поэтому на iPhone подходит
только ELM327 с **Bluetooth LE** (обычно пишут «Bluetooth 4.0 / iOS») или **Wi‑Fi**.

## Вариант 1 — Pythonista 3 (рекомендуется: интерфейс, BLE и Wi‑Fi)

1. Установите **Pythonista 3** из App Store.
2. Получите код VagOn в Pythonista — любым способом:
   * **Установщик:** создайте в Pythonista новый файл `install_vagon.py`, вставьте содержимое
     [`scripts/ios/install_vagon.py`](../scripts/ios/install_vagon.py) и запустите (▶). Он скачает
     папку `vagon` и `VagOn.py` с GitHub. Если репозиторий приватный — впишите в `TOKEN` токен GitHub
     (Settings → Developer settings → Fine‑grained token, доступ Contents: Read) или используйте способ ниже.
   * **Файлы:** на Mac скопируйте папку `vagon/` и файл `scripts/ios/VagOn.py` в iCloud Drive,
     затем в приложении «Файлы» перенесите их в `На iPhone → Pythonista 3` (обе вещи — в одну папку).
3. Вставьте адаптер в OBD, включите зажигание.
4. В Pythonista откройте `VagOn.py` и нажмите ▶. Откроется интерфейс VagOn на весь экран.
5. Вкладка **Связь** → «Bluetooth LE» (или «Wi‑Fi», если адаптер Wi‑Fi и iPhone подключён к его сети)
   → «Подключить» → «Найти блоки».

Закрыть — крестик в углу (сервер останавливается вместе с окном). Обновить VagOn — снова запустить
`install_vagon.py`: ваши копии и рецепты (папка `VagOn/` в Документах Pythonista) не трогаются.

Примечания:
* При первом подключении iOS спросит доступ к Bluetooth / локальной сети — разрешите.
* Пока идёт запись, не сворачивайте Pythonista и не блокируйте экран: iOS приостанавливает фоновые
  приложения. VagOn всё равно проверяет результат, но лучше не прерывать.
* Резервные копии лежат в `Файлы → На iPhone → Pythonista 3 → VagOn/backups`.

## Вариант 2 — a‑Shell (бесплатно, только командная строка и Wi‑Fi)

1. Установите **a‑Shell** из App Store.
2. В a‑Shell:
   ```sh
   lg2 clone https://github.com/AlexanderSobin/VagOn.git
   cd VagOn
   ```
3. Подключите iPhone к Wi‑Fi адаптера и запускайте команды как на Mac:
   ```sh
   python3 -m vagon -a wifi scan
   python3 -m vagon -a wifi recipe check mmi.wireless_carplay
   ```
BLE в a‑Shell недоступен. Веб‑интерфейс в a‑Shell неудобен: iOS останавливает a‑Shell, когда вы
переключаетесь в Safari.

## Если адаптер не находится по BLE

* Убедитесь, что он не подключён к другому приложению (Car Scanner, OBDeleven…) — BLE‑адаптер
  работает только с одним приложением одновременно.
* Укажите имя явно: вкладка «Связь» → «Другой» → `ble://ИМЯ` (имя видно в списке Bluetooth‑устройств
  других приложений, часто `OBDII`, `IOS-Vlink`, `V-LINK`).
* Некоторые «Bluetooth» адаптеры — только классический Bluetooth. С iPhone они не работают;
  используйте их с Mac (`serial:///dev/cu.…`).
