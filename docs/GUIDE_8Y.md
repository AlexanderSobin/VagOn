# Audi A3 8Y — пошагово

## 0. Один раз: подготовка

1. Потренируйтесь на симуляторе: `vagon -a sim scan`, `vagon -a sim recipe on mmi.wireless_carplay`,
   вкладка «Обучение» в `vagon -a sim web --open`. Всё ведёт себя так же, как на машине.
2. В машине: зажигание ВКЛ, двигатель ВЫКЛ, зарядное подключено (или двигатель заведён только для
   чтения — для записи глушите).
3. Проверка адаптера и машины:
   ```bash
   vagon info            # «Итог: адаптер подходит для кодирования VAG»
   vagon scan --dtc      # список блоков, сохраните вывод
   vagon backup-all      # копия кодирования ВСЕХ блоков — до любых изменений
   vagon recipes --check # что уже включено, что требует обучения
   ```

## 1. SFD: разблокировка перед записью

Почти все интересные блоки 8Y защищены SFD. Если VagOn пишет
«Логин принят, но запись запрещена — почти наверняка защита SFD», значит, перед записью нужно
разблокировать блок легальным способом (OBDeleven Pro/Ultimate → блок → «SFD unlock»; VCDS с
токеном; ODIS с GeKo), **не выключая зажигание** переключиться на VagOn и записать в течение окна
разблокировки. Если в окне запись всё равно не проходит — значит, ваш блок принимает запись только от
того инструмента, который его разблокировал (или это SFD2), и сделать изменение придётся им.

## 2. Беспроводной CarPlay (`mmi.wireless_carplay`)

Условия: MIB3 и работающий **проводной** CarPlay. Перед записью удалите iPhone из списка CarPlay и
Bluetooth в MMI.

Каналы рецепта нужно один раз выучить на вашем 5F. Самый надёжный путь — записать изменение,
сделанное в ODIS (VNCI) или OBDeleven:

```bash
vagon learn start 5F                 # снимок «до» (первый раз — опрос блока, ~10–20 мин)
#  → ODIS / OBDeleven: 5F → Адаптация → включить каналы беспроводного CarPlay
vagon learn finish 5F --recipe-name "Беспроводной CarPlay (мой)"
```
После этого `vagon recipe off/on user.besprovodnoy_carplay_moy` выключает/включает это с Mac или
iPhone (при SFD — во время разблокировки). Хотите, чтобы заработал встроенный рецепт с проверкой
состояния — учите каналы по одному (`--name 5_ghz_wlan --old off --new on`, затем
`Wifi_client_mode_5GHz`, …).

Затем: перезагрузка MMI (удерживать кнопку питания MMI ~10–15 с), заново подключить iPhone.

## 3. Анимация задних фонарей RS3 (`lights.rs3_taillight_animation`)

Канал 09 «Intelligente Heckleuchte‑Verbau»: 1 (обычно с завода) → 5. Работает, если у фонарей есть
базовая анимация (значение не 0); по отзывам — с MMI 34xx, а на 3606/3617/3620 часто нет эффекта.

```bash
vagon learn start 09
#  → ODIS/OBDeleven: 09 → Адаптация → Intelligente Heckleuchte-Verbau → 5
vagon learn finish 09 --name "Intelligente Heckleuchte-Verbau" --old 1 --new 5
vagon recipe check lights.rs3_taillight_animation
```
Откат: `vagon recipe off lights.rs3_taillight_animation` (вернёт значение из копии).

## 4. Coming home / Leaving home и другие настройки из меню MMI

То, что можно менять в меню MMI (длительность Coming/Leaving home, подсветка, зеркала…), учится без
сторонних инструментов:
```bash
vagon learn start 09
#  MMI: Свет → Coming home → 30 с   (было 10 с)
vagon learn finish 09 --name "Coming home duration" --old 10 --new 30
vagon labels list 09          # теперь видно текущее значение
```
Дальше значение можно менять из VagOn (веб: «Блоки → 09 → канал»), в том числе на те, что
блок принимает, но меню не предлагает (блок сам отклонит недопустимое значение — NRC 0x31).

## 5. Приветствие стрелками (`cockpit.needle_sweep`)

Это бит длинного кодирования 17 (Byte 1 Bit 0) — работает без обучения:
```bash
vagon recipe check cockpit.needle_sweep
vagon recipe on cockpit.needle_sweep
```
На цифровой панели 8Y эффект часто не виден — тогда `vagon recipe off cockpit.needle_sweep`.

## 6. Что-то пошло не так

* `vagon journal` — что и когда записывалось.
* `vagon backups 5F` → `vagon restore <файл>` — вернуть блок к любой копии.
* Полная копия из шага 0 позволяет вернуть кодирование любого блока к исходному.
* После изменений в 5F/17 перезагрузите MMI, в 09 — выключите и включите зажигание, закройте/откройте
  машину.
* Ошибки после экспериментов: `vagon dtc` → `vagon dtc 09 --clear`.
