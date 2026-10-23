# Формат рецептов

Рецепт — JSON‑описание твика. Встроенные: `vagon/data/recipes/*.json`; свои кладите в
`~/.vagon/recipes/*.json` (iPhone: `Pythonista → VagOn/recipes`). Свой рецепт с тем же `id`
заменяет встроенный.

```json
{
  "vagon_recipes": 1,
  "recipes": [
    {
      "id": "my.coming_home_60",
      "category": "lights",
      "title": {"ru": "Coming home 60 с", "en": "Coming home 60 s"},
      "description": {"ru": "…"},
      "module": "09",
      "status": "user",
      "sfd": "yes",
      "login": 31347,
      "session": "0x03",
      "requires": {"dataset_prefix": ["EV_BCM"], "part_prefix": ["5WA937"], "sw_not": ["0100"]},
      "changes": [
        {"kind": "channel", "channel": "Coming home duration", "on": "60", "off": "30"},
        {"kind": "coding_bit", "byte": 12, "bit": 3, "on": 1, "off": 0},
        {"kind": "coding_bits", "byte": 4, "mask": "0x0C", "on": "0x04", "off": "0x00"},
        {"kind": "did_bits", "did": "0x0961", "byte": 0, "mask": "0x01", "on": "0x01", "off": "0x00"},
        {"kind": "did_bytes", "did": "0x0905", "offset": 0, "on": "79", "off": "4C"},
        {"kind": "channel", "module": "17", "channel": "Laptimer", "alt": ["Lap_timer"], "on": "activated", "optional": true}
      ],
      "notes": {"ru": "…"},
      "after": {"ru": "Перезагрузите MMI."},
      "sources": ["https://…"]
    }
  ]
}
```

| Поле | Смысл |
|---|---|
| `module` | Блок по умолчанию (адрес VCDS: `09`, `17`, `5F`…). У изменения можно указать свой `module`. |
| `login` | Логин‑код Security Access. Число или `{"5F": 20103, "09": 31347}`. Если не указан и блок ответил «доступ запрещён», VagOn один раз пробует типовой код этого блока. |
| `session` | Диагностическая сессия: `0x03` (расширенная, по умолчанию) или `0x4F` (разработчика). |
| `requires` | Условия: префиксы ODX‑датасета (F19E), номера детали (F187), версии ПО, которые **не** подходят, длина кодирования. Не выполнено — рецепт не применяется. |
| `status` | `tested`, `community`, `community-mqb`, `needs-labels`, `user`. |
| `sfd` | `yes` / `likely` / `no` / `unknown` — для предупреждения. |

Типы изменений:

* `coding_bit` — бит длинного кодирования (DID 0x0600). Байт 0 — первый, бит 0 — младший (0x01),
  как в VCDS/OBDeleven.
* `coding_bits` — несколько битов байта кодирования по маске; `on`/`off` — значение **уже на своих
  местах** в маске (для маски `0x0C` значение `0x04` = биты 3..2 = `01`).
* `did_bits` — то же для любого DID.
* `did_bytes` — заменить байты DID начиная с `offset`.
* `channel` — канал адаптации по имени; разрешается через метки (выученные или импортированные) для
  ODX‑датасета вашего блока. `alt` — альтернативные имена в других версиях ПО, `optional` — пропустить
  с предупреждением, если канала нет. Значения: имя (`activated`, `on`…), число или физическое
  значение для числовых каналов (`12.1`).

`off` можно не указывать: тогда выключение берёт исходное значение из резервной копии, сделанной при
включении этим рецептом.

## Метки каналов вручную

Если вы знаете DID и биты канала (например, из ODX или чужого лога):
```bash
vagon labels add 5F --name "Developer mode" --did 0x1234 --byte 0 --bit 0 --bits 1 \
      --values "not activated=0,activated=1" --session 0x4F
```
Метка сохраняется для ODX‑датасета подключённого блока.
