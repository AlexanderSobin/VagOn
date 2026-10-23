"""VagOn command line. Run `python3 -m vagon --help`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

from . import __version__
from .app import App
from .errors import NegativeResponse, SafetyError, TimeoutError_, VagOnError
from .hexutil import parse_hex, parse_int, to_hex
from .paths import data_dir
from .recipes import STATUSES, apply_plan, build_learned_recipe, plan_recipe, save_user_recipe
from .vag.coding import LongCoding
from .vag.dtc import describe_status, format_dtc
from .vag.labels import Channel, save_user_channel
from .vag.modules import get_module
from .vag.vehicle import did_key

# ------------------------------------------------------------------ helpers


def out(text: str = "") -> None:
    print(text, flush=True)


def confirm(question: str, yes: bool) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        out("Нет подтверждения: запустите с --yes, чтобы записать.")
        return False
    try:
        ans = input(f"{question} [да/нет]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("да", "д", "yes", "y")


def progress(msg: str) -> None:
    out(f"  … {msg}")


def print_write_result(r) -> None:
    if r.dry_run:
        out(f"[{r.module}] пробный прогон, ничего не записано")
    for did, (old, new) in r.written.items():
        out(f"[{r.module}] {did}: {old} → {new}")
    for n in r.notes:
        out(f"[{r.module}] примечание: {n}")
    if r.backup_path:
        out(f"[{r.module}] резервная копия: {r.backup_path}")
    if r.verified and r.written and not r.dry_run:
        out(f"[{r.module}] ✔ записано и проверено чтением")


def print_plan(plan) -> None:
    r = plan.recipe
    action = "ВКЛЮЧИТЬ" if plan.action == "on" else "ВЫКЛЮЧИТЬ"
    out(f"Рецепт: {r.title.get('ru', r.id)}  [{r.id}]  → {action}")
    out(f"Статус рецепта: {STATUSES.get(r.status, r.status)}")
    state = {"on": "включено", "off": "выключено", "partial": "частично", "unknown": "неизвестно"}
    out(f"Сейчас в машине: {state.get(plan.state, plan.state)}")
    for mp in plan.modules:
        mod = get_module(mp.module)
        extra = []
        if mp.login is not None:
            extra.append(f"логин {mp.login}")
        if mp.session != 0x03:
            extra.append(f"сессия 0x{mp.session:02X}")
        out(f"  Блок {mod.label()}" + (f" ({', '.join(extra)})" if extra else ""))
        if mp.ident:
            out(f"    {mp.ident.get('part_number', '')}  SW {mp.ident.get('sw_version', '')}  "
                f"{mp.ident.get('odx_dataset', '')}")
        for ln in mp.lines:
            out(f"    • {ln}")
        for did, (old, new) in mp.writes.items():
            out(f"    запись {did_key(did)}: {old.hex().upper()} → {new.hex().upper()}")
    for w in plan.warnings:
        out(f"  ⚠ {w}")
    for b in plan.blockers:
        out(f"  ✖ {b}")
    if plan.nothing_to_do:
        out("  Ничего менять не нужно.")


def parse_did(text: str) -> int:
    s = text.strip()
    return parse_int(s if s.lower().startswith("0x") else "0x" + s, bits=16)


_TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                     ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p",
                      "r", "s", "t", "u", "f", "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slug(text: str) -> str:
    low = "".join(_TRANSLIT.get(c, c) for c in text.lower())
    s = re.sub(r"[^a-z0-9]+", "_", low).strip("_")
    return s[:40] or "recipe"


# ------------------------------------------------------------------ commands


def cmd_adapters(app: App, a) -> int:
    from .transport.links import list_serial_ports
    out("Последовательные порты (USB / Bluetooth classic на Mac):")
    ports = list_serial_ports()
    for p in ports:
        out(f"  {p}")
    if not ports:
        out("  (нет)")
    if a.ble:
        try:
            from .transport.ble import bleak_scan, looks_like_obd
            out("BLE-устройства (8 с)…")
            for name, addr in bleak_scan(8.0):
                mark = "  ← похоже на OBD" if looks_like_obd(name) else ""
                out(f"  {name:24s} {addr}{mark}")
        except VagOnError as e:
            out(f"  BLE: {e}")
    out("\nWi-Fi адаптер: подключитесь к его сети и используйте --adapter wifi "
        "(или tcp://IP:ПОРТ). Симулятор: --adapter sim")
    return 0


def cmd_info(app: App, a) -> int:
    v = app.connect()
    info = v.transport.info()
    out(f"Адаптер: {info.get('description')}")
    if info.get("device"):
        out(f"Устройство: {info['device']}")
    out(f"Чип STN: {'да' if info.get('stn') else 'нет'}")
    if info.get("voltage") is not None:
        volt = info["voltage"]
        warn = "  ⚠ низкое! подключите зарядное перед записью" if volt < 12.0 else ""
        out(f"Напряжение: {volt:.1f} В{warn}")
    vin = v.read_vin()
    out(f"VIN: {vin or 'не прочитан (зажигание включено?)'}")
    from .vag.check import adapter_check, format_check
    out("Проверка адаптера для VAG:")
    for ln in format_check(adapter_check(v)):
        out(f"  {ln}")
    out(f"Данные VagOn: {data_dir()}")
    return 0


def cmd_scan(app: App, a) -> int:
    v = app.connect()
    out("Опрашиваю блоки…")
    found = v.scan(progress=None if a.quiet else progress, with_dtc=a.dtc)
    out("")
    if v.vin:
        out(f"VIN: {v.vin}")
    n = 0
    for e in found:
        if not e.found:
            continue
        n += 1
        i = e.ident
        dtc = f"  ошибок: {e.dtc_count}" if e.dtc_count is not None else ""
        out(f"{e.module.addr_hex}  {e.module.name_ru:38s} {i.part_number:12s} SW {i.sw_version:5s} "
            f"{i.odx_dataset}{dtc}")
    out(f"\nНайдено блоков: {n}")
    return 0


def cmd_ident(app: App, a) -> int:
    v = app.connect()
    s = v.module(a.module)
    i = s.ident(refresh=True)
    out(f"Блок {s.module.label()}  (CAN {s.module.tx_id:03X}/{s.module.rx_id:03X})")
    for k, label in (("part_number", "Номер детали"), ("sw_version", "Версия ПО"),
                     ("hw_number", "Номер железа"), ("hw_version", "Версия железа"),
                     ("system_name", "Система"), ("odx_dataset", "ODX датасет"),
                     ("odx_version", "Версия ODX"), ("serial", "Серийный №"), ("vin", "VIN"),
                     ("coding", "Кодирование"), ("workshop_code", "Код мастерской (F1A5)")):
        val = getattr(i, k)
        if val:
            out(f"  {label:22s} {val}")
    for k, err in i.errors.items():
        out(f"  ({k}: {err})")
    return 0


def cmd_coding(app: App, a) -> int:
    v = app.connect()
    s = v.module(a.module)
    lc = s.read_coding()
    if a.action in (None, "show"):
        out(f"Блок {s.module.label()}: длинное кодирование, {len(lc)} байт")
        out(lc.hex())
        out(lc.table())
        return 0
    new = lc.copy()
    if a.action == "set":
        if len(a.args) != 3 or a.args[2].lower() not in ("on", "off", "1", "0", "вкл", "выкл", "true", "false"):
            raise SafetyError("формат: coding МОД set БАЙТ БИТ on|off")
        new.set_bit(int(a.args[0]), int(a.args[1]), a.args[2].lower() in ("on", "1", "вкл", "true"))
    elif a.action == "write":
        new = LongCoding(parse_hex(" ".join(a.args)))
        if len(new) != len(lc):
            raise SafetyError(f"длина новой кодировки {len(new)} ≠ {len(lc)} — отмена")
    changes = lc.diff(new)
    if not changes:
        out("Ничего не меняется.")
        return 0
    out(f"Блок {s.module.label()}:")
    out(f"  было:  {lc.hex()}")
    out(f"  будет: {new.hex()}")
    for c in changes:
        out(f"  • {c}")
    if not confirm("Записать кодирование?", a.yes):
        return 1
    r = s.write_coding(new, reason="manual coding", login=a.login, progress=progress)
    print_write_result(r)
    return 0


def cmd_did(app: App, a) -> int:
    v = app.connect()
    s = v.module(a.module)
    if a.action == "read":
        did = parse_did(a.args[0])
        data = s.read_did(did)
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in data)
        out(f"{did_key(did)} = {to_hex(data)}   '{text}'  ({len(data)} байт)")
        return 0
    if a.action == "write":
        did = parse_did(a.args[0])
        data = parse_hex(" ".join(a.args[1:]))
        cur = s.read_did(did)
        out(f"{did_key(did)}: {to_hex(cur)} → {to_hex(data)}")
        if not confirm("Записать?", a.yes):
            return 1
        session = parse_int(a.session) if a.session else 0x03
        r = s.write({did: data}, reason=f"manual did {did_key(did)}", login=a.login, session=session,
                    progress=progress)
        print_write_result(r)
        return 0
    if a.action == "sweep":
        from .vag.learn import parse_ranges, sweep
        ranges = parse_ranges(a.ranges)
        total = sum(hi - lo + 1 for lo, hi in ranges)
        out(f"Опрос {total} идентификаторов блока {s.module.addr_hex} (только чтение, можно прервать Ctrl+C "
            "и продолжить позже)…")
        last = [0.0]

        def prog(i, n, found):
            if time.monotonic() - last[0] > 2 or i == n:
                last[0] = time.monotonic()
                out(f"  {i}/{n}, найдено {found}")
        try:
            data = sweep(s, ranges, vin=v.vin or v.read_vin() or "UNKNOWN", progress=prog)
        except KeyboardInterrupt:
            out("Прервано — прогресс сохранён.")
            return 130
        for k, val in sorted(data["dids"].items()):
            out(f"  {k} = {val}")
        return 0
    raise SafetyError("did read|write|sweep")


def cmd_dtc(app: App, a) -> int:
    v = app.connect()
    mods = a.modules or [e.module.addr_hex for e in (v.found or v.scan()) if e.found]
    total = 0
    for m in mods:
        s = v.module(m)
        try:
            dtcs = s.read_dtcs()
        except TimeoutError_:
            continue
        except NegativeResponse as e:
            out(f"{s.module.label()}: {e}")
            continue
        if not dtcs and not a.all:
            continue
        out(f"{s.module.label()}: {len(dtcs)} ошиб.")
        for code, status in dtcs:
            out(f"   {format_dtc(code)}  (0x{code:06X}, статус 0x{status:02X}: {describe_status(status)})")
        total += len(dtcs)
        if a.clear and dtcs:
            if confirm(f"Стереть ошибки в {s.module.addr_hex}?", a.yes):
                s.clear_dtcs()
                out("   стёрто")
    out(f"Всего ошибок: {total}")
    return 0


def cmd_recipes(app: App, a) -> int:
    v = app.connect() if a.check else None
    cat = None
    for r in app.book.list(a.category):
        if r.category != cat:
            cat = r.category
            out(f"\n== {cat} ==")
        state = ""
        if v is not None:
            try:
                p = plan_recipe(v, r, "on", app.labels)
                state = {"on": "[ВКЛ] ", "off": "[выкл]", "partial": "[част]"}.get(p.state, "[ ?  ]")
                if p.blockers:
                    state = "[нужно обучение]" if any("канал" in b for b in p.blockers) else "[недоступно]"
            except VagOnError:
                state = "[ошибка]"
        out(f"  {state} {r.id:34s} {r.title.get('ru', '')}  ({r.status}, блок {', '.join(r.modules)})")
    return 0


def cmd_recipe(app: App, a) -> int:
    r = app.book.get(a.id)
    if a.action == "show":
        out(json.dumps(r.to_summary(), ensure_ascii=False, indent=2))
        return 0
    v = app.connect()
    action = {"check": "on", "on": "on", "off": "off"}[a.action]
    plan = plan_recipe(v, r, action, app.labels)
    print_plan(plan)
    if a.action == "check" or plan.blockers or plan.nothing_to_do:
        return 0 if not plan.blockers else 2
    if not confirm("Применить?", a.yes):
        return 1
    for res in apply_plan(v, plan, progress=progress, force_login=a.force_login):
        print_write_result(res)
    after = r.after.get("ru") if isinstance(r.after, dict) else r.after
    if after and action == "on":
        out(f"Дальше: {after}")
    return 0


def cmd_backups(app: App, a) -> int:
    for b in app.store.list_backups(module=a.module):
        out(f"{b.created}  блок {b.module}  {b.data.get('reason', '')}\n    {b.path}")
    return 0


def cmd_backup_all(app: App, a) -> int:
    """Identification + long coding (+ known adaptation DIDs) of every module, read-only."""
    from .vag.learn import IGNORED_DIDS, load_known_dids
    v = app.connect()
    found = [e for e in (v.found or v.scan(progress=None if a.quiet else progress)) if e.found]
    vin = v.vin or v.read_vin() or "UNKNOWN"
    saved = 0
    for e in found:
        s = v.module(e.module.addr_hex)
        try:
            ident = s.ident(refresh=True)
        except (TimeoutError_, NegativeResponse) as err:
            out(f"  {e.module.label()}: пропуск ({err})")
            continue
        dids = {}
        if ident.coding:
            dids["0x0600"] = ident.coding
        known = load_known_dids(vin, s.module.addr_hex, ident.odx_dataset)
        for k in known.get("dids", {}):
            if int(k, 16) in IGNORED_DIDS:
                continue  # identification / workshop data is not restored
            try:
                dids[k] = s.read_did(int(k, 16)).hex().upper()
            except (TimeoutError_, NegativeResponse):
                continue
        b = app.store.save_backup(vin=vin, module=s.module.addr_hex, ident=ident.to_dict(), coding=ident.coding,
                                  dids=dids, reason="full backup")
        saved += 1
        out(f"  {e.module.label():45s} {len(dids):3d} знач. → {os.path.basename(b.path)}")
    out(f"Сохранено блоков: {saved}. Папка: {os.path.join(data_dir(), 'backups')}")
    return 0


def diff_backups(old, new) -> list[str]:
    """Human-readable differences between two backups of the same module."""
    lines = []
    for k in sorted(set(old.dids) | set(new.dids)):
        a_hex, b_hex = old.dids.get(k), new.dids.get(k)
        if a_hex == b_hex:
            continue
        if a_hex is None or b_hex is None:
            lines.append(f"{k}: {a_hex or '—'} → {b_hex or '—'}")
            continue
        a_b, b_b = bytes.fromhex(a_hex), bytes.fromhex(b_hex)
        lines.append(f"{k}: {a_hex} → {b_hex}")
        if len(a_b) == len(b_b):
            for i, (x, y) in enumerate(zip(a_b, b_b)):
                for bit in range(8):
                    if (x ^ y) >> bit & 1:
                        lines.append(f"    Byte {i} Bit {bit}: {(x >> bit) & 1} → {(y >> bit) & 1}")
    return lines


def cmd_diff(app: App, a) -> int:
    from .vag.store import Store
    if a.files and len(a.files) == 2:
        pairs = [(Store.load_backup(a.files[0]), Store.load_backup(a.files[1]))]
    elif not a.files:
        pairs = []
        by_mod: dict[str, list] = {}
        for b in app.store.list_backups(module=a.module):
            if a.any or b.data.get("reason") == "full backup":
                by_mod.setdefault(b.module, []).append(b)
        for mod, items in sorted(by_mod.items()):
            if len(items) >= 2:
                pairs.append((items[-2], items[-1]))
    else:
        raise SafetyError("нужно два файла копий или ни одного (тогда сравниваются две последние копии)")
    shown = 0
    for old, new in pairs:
        lines = diff_backups(old, new)
        if not lines:
            continue
        shown += 1
        out(f"Блок {new.module}: {old.created} ({old.data.get('reason', '')}) → "
            f"{new.created} ({new.data.get('reason', '')})")
        for ln in lines:
            out(f"  {ln}")
    if not shown:
        out("Различий нет.")
    return 0


def cmd_restore(app: App, a) -> int:
    from .vag.store import Store
    b = Store.load_backup(a.file)
    v = app.connect()
    s = v.module(b.module)
    kw = dict(login=a.login, coding_only=a.coding_only, force=a.force)
    res = s.restore(b, dry_run=True, **kw)
    for did, (old, new) in res.written.items():
        out(f"[{b.module}] {did}: {old} → {new}")
    if not res.written:
        out("Блок уже в состоянии копии.")
        return 0
    out("Идентификаторы, которые блок не принимает (живые значения, только чтение), будут пропущены.")
    if not confirm("Восстановить?", a.yes):
        return 1
    print_write_result(s.restore(b, progress=progress, **kw))
    return 0


def _known_dids(v, s, ranges_text):
    from .vag.learn import load_known_dids, parse_ranges, sweep
    ident = s.ident(refresh=True)
    vin = v.vin or v.read_vin() or "UNKNOWN"
    ranges = parse_ranges(ranges_text)
    done = set(load_known_dids(vin, s.module.addr_hex, ident.odx_dataset).get("done", []))
    remaining = sum(1 for lo, hi in ranges for d in range(lo, hi + 1) if d not in done)
    if remaining:
        out(f"Узнаю, какие идентификаторы есть у блока: {remaining} шт. (однократно, только чтение, "
            "прерванный опрос продолжится с места остановки)…")
    last = [0.0]

    def prog(i, n, found):
        if time.monotonic() - last[0] > 3 or i == n:
            last[0] = time.monotonic()
            out(f"  {i}/{n}, найдено {found}")
    data = sweep(s, ranges, vin=vin, progress=prog)     # resumes; no-op when complete
    return [int(k, 16) for k in data["dids"]]


def cmd_learn(app: App, a) -> int:
    from .vag.learn import LearnSession, infer_channel
    v = app.connect()
    s = v.module(a.module)
    if a.action == "start":
        dids = _known_dids(v, s, a.ranges)
        ls = LearnSession.start(s, dids, v.vin or "")
        out(f"Снимок сделан: {len(ls.data['before'])} идентификаторов "
            f"(живые значения исключены: {len(ls.data['noisy'])}).")
        out("Теперь измените ОДНУ настройку (в меню MMI, ODIS, OBDeleven…), затем выполните:")
        out(f"  vagon learn finish {s.module.addr_hex} --name \"Имя канала\" --old \"было\" --new \"стало\"")
        out(f"  или: vagon learn finish {s.module.addr_hex} --recipe-name \"Мой твик\"")
        return 0
    ls = LearnSession.load(s.module.addr_hex)
    changes = ls.finish(s)
    if not changes:
        out("Изменений не найдено (снимок «до» сохранён — можно изменить настройку и снова выполнить finish). "
            "Возможно, изменение в другом блоке или в DID вне опрошенных диапазонов (--ranges full).")
        return 1
    out("Изменились:")
    for c in changes:
        bits = ", ".join(f"байт {b} бит {bit}" for b, bit in c.changed_bits())
        out(f"  {did_key(c.did)}: {c.old.hex().upper()} → {c.new.hex().upper()}   ({bits})")
    ident = s.ident(refresh=True)
    if a.name:
        pick = changes
        if a.did:
            pick = [c for c in changes if c.did == parse_did(a.did)]
        if len(pick) != 1:
            out("Изменилось несколько идентификаторов — укажите нужный: --did 0xXXXX "
                "(или используйте --recipe-name, чтобы сохранить всё целиком).")
            return 2
        ch = infer_channel(pick[0], a.name, a.old or "", a.new or "",
                           source=f"learn {time.strftime('%Y-%m-%d')}", bit=a.bit, bits=a.bits,
                           byte=a.byte)
        if a.session:
            ch.session = parse_int(a.session)
        path = save_user_channel(s.module.addr_hex, ident.odx_dataset, ch)
        out(f"Метка «{ch.name}» сохранена: DID {did_key(ch.did)}, байт {ch.byte}, бит {ch.bit}, "
            f"ширина {ch.bits}, значения {ch.values or f'scale={ch.scale:g}'} → {path}")
        app.reload()
        users = [r.id for r in app.book.list() if any(
            c.kind == "channel" and c.module == s.module.addr_hex and
            a.name.lower() in [c.spec["channel"].lower()] + [x.lower() for x in c.spec.get("alt", [])]
            for c in r.changes)]
        if users:
            out(f"Теперь этот канал знают рецепты: {', '.join(users)}")
    if a.recipe_name:
        rid = a.recipe_id or f"user.{slug(a.recipe_name)}"
        recipe = build_learned_recipe(rid, a.recipe_name, s.module.addr_hex, changes, ident, login=a.login,
                                      session=parse_int(a.session) if a.session else None)
        path = save_user_recipe(recipe)
        out(f"Рецепт {rid} сохранён в {path}. Включить/выключить: vagon recipe on|off {rid}")
    if a.name or a.recipe_name:
        ls.commit()
    else:
        out("Подсказка: добавьте --name … или --recipe-name …, чтобы сохранить результат "
            "(снимок «до» пока сохранён).")
    return 0


def cmd_labels(app: App, a) -> int:
    if a.action == "list":
        v = app.connect()
        s = v.module(a.module)
        ident = s.ident()
        chans = app.labels.channels(s.module.addr_hex, ident.odx_dataset)
        out(f"Блок {s.module.label()}, датасет {ident.odx_dataset}: известно каналов {len(chans)}")
        for ch in chans:
            try:
                raw = ch.extract(s.read_did(ch.did))
                val = ch.value_name(raw)
            except (NegativeResponse, TimeoutError_, ValueError) as e:
                val = f"(не прочитан: {e})"
            out(f"  {ch.name:45s} = {val:20s} DID {did_key(ch.did)} б{ch.byte} бит{ch.bit}/{ch.bits}")
        return 0
    if a.action == "add":
        v = app.connect()
        s = v.module(a.module)
        ident = s.ident()
        values = {}
        for part in (a.values or "").split(","):
            if "=" in part:
                k, val = part.split("=", 1)
                values[k.strip()] = parse_int(val.strip())
        ch = Channel(name=a.name, did=parse_did(a.did), byte=a.byte, bit=a.bit, bits=a.bits, values=values,
                     session=parse_int(a.session) if a.session else None, source="manual")
        out(f"Сохранено: {save_user_channel(s.module.addr_hex, ident.odx_dataset, ch)}")
        return 0
    if a.action == "import-odx":
        from .vag.odx_import import import_odx
        path, n = import_odx(a.file, module=a.module)
        out(f"Импортировано каналов: {n} → {path}")
        return 0
    raise SafetyError("labels list|add|import-odx")


def cmd_journal(app: App, a) -> int:
    for e in app.store.read_journal(a.limit):
        out(f"{e.get('time')}  {e.get('module')}  {e.get('status'):12s} {e.get('reason', '')}")
        for did, (old, new) in (e.get("changes") or {}).items():
            out(f"      {did}: {old} → {new}")
        if e.get("error"):
            out(f"      ошибка: {e['error'][:200]}")
    return 0


def cmd_web(app: App, a) -> int:
    from .web.server import serve
    serve(app, host=a.host, port=a.port, open_browser=a.open)
    return 0


def cmd_sim(app: App, a) -> int:
    from .sim.car import make_sim_elm
    from .sim.link import serve_tcp
    sfd = (a.sfd or "").upper()
    out(f"Симулятор Wi-Fi ELM327 + Audi A3 8Y на {a.host}:{a.port} (Ctrl+C — выход)")
    out(f"Подключение: vagon --adapter tcp://{a.host}:{a.port} scan")
    try:
        serve_tcp(lambda: make_sim_elm(sfd_5f="5F" in sfd, sfd_17="17" in sfd), host=a.host, port=a.port)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_sim_reset(app: App, a) -> int:
    path = os.path.join(data_dir(), "sim_state.json")
    if os.path.exists(path):
        os.remove(path)
    out("Состояние симулятора сброшено.")
    return 0


def cmd_sim_change(app: App, a) -> int:
    """Emulate a change made by another tool (ODIS/OBDeleven) — simulator only."""
    if not app.sim:
        raise SafetyError("sim-change работает только с --adapter sim")
    from .sim.car import build_a3_8y, load_state, save_state
    path = os.path.join(data_dir(), "sim_state.json")
    ecus = build_a3_8y()
    load_state(ecus, path)
    m = get_module(a.module)
    ecu = next(e for e in ecus if e.address == m.address)
    did = parse_did(a.did)
    data = parse_hex(a.hex)
    if did not in ecu.dids or len(ecu.dids[did]) != len(data):
        raise SafetyError("нет такого DID или другая длина")
    ecu.dids[did] = data
    save_state(ecus, path)
    out(f"[симулятор] {m.addr_hex} {did_key(did)} = {to_hex(data)}")
    return 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vagon", description="VagOn — кодирование и адаптации VAG (Audi A3 8Y и др.) "
                                "через ELM327 на Mac и iPhone.")
    p.add_argument("--version", action="version", version=f"VagOn {__version__}")
    p.add_argument("-a", "--adapter", help="wifi | ble | ble://ИМЯ | tcp://IP:ПОРТ | serial:///dev/cu.X | sim "
                   "(по умолчанию $VAGON_ADAPTER или wifi)")
    p.add_argument("--stmin", type=int, default=10, help="пауза между кадрами от блока, мс (по умолчанию 10)")
    p.add_argument("--no-trace", action="store_true", help="не писать журнал обмена с адаптером")
    p.add_argument("--min-voltage", type=float, default=11.5,
                   help="не писать при напряжении ниже (В, по умолчанию 11.5; 0 — не проверять)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        sp = sub.add_parser(name, help=help_)
        sp.set_defaults(fn=fn)
        return sp

    sp = add("adapters", cmd_adapters, "найти адаптеры (порты, BLE)")
    sp.add_argument("--ble", action="store_true", help="сканировать BLE (нужен bleak)")
    add("info", cmd_info, "адаптер, напряжение, VIN")
    sp = add("scan", cmd_scan, "найти блоки в машине")
    sp.add_argument("--dtc", action="store_true", help="заодно посчитать ошибки")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp = add("ident", cmd_ident, "идентификация блока")
    sp.add_argument("module")

    sp = add("coding", cmd_coding, "длинное кодирование: показать / изменить бит / записать")
    sp.add_argument("module")
    sp.add_argument("action", nargs="?", choices=["show", "set", "write"])
    sp.add_argument("args", nargs="*", help="set BYTE BIT on|off  |  write HEX")
    sp.add_argument("--login", type=int)
    sp.add_argument("--yes", action="store_true")

    sp = add("did", cmd_did, "идентификаторы данных: read DID | write DID HEX | sweep")
    sp.add_argument("module")
    sp.add_argument("action", choices=["read", "write", "sweep"])
    sp.add_argument("args", nargs="*")
    sp.add_argument("--login", type=int)
    sp.add_argument("--session", help="например 0x4F (сессия разработчика)")
    sp.add_argument("--ranges", help="для sweep: default | full | 0000-0FFF,2000-3FFF")
    sp.add_argument("--yes", action="store_true")

    sp = add("dtc", cmd_dtc, "ошибки блоков (и стирание)")
    sp.add_argument("modules", nargs="*")
    sp.add_argument("--clear", action="store_true")
    sp.add_argument("--all", action="store_true", help="показывать и блоки без ошибок")
    sp.add_argument("--yes", action="store_true")

    sp = add("recipes", cmd_recipes, "список рецептов (твиков)")
    sp.add_argument("--category")
    sp.add_argument("--check", action="store_true", help="проверить состояние на машине")
    sp = add("recipe", cmd_recipe, "рецепт: show | check | on | off")
    sp.add_argument("action", choices=["show", "check", "on", "off"])
    sp.add_argument("id")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--force-login", action="store_true", help="повторить логин даже после отказа")

    sp = add("backups", cmd_backups, "резервные копии")
    sp.add_argument("module", nargs="?")
    sp = add("backup-all", cmd_backup_all, "резервная копия кодирования всех блоков (только чтение)")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp = add("diff", cmd_diff, "сравнить копии: два файла или две последние полные копии (backup-all)")
    sp.add_argument("files", nargs="*")
    sp.add_argument("--module")
    sp.add_argument("--any", action="store_true", help="сравнивать и автоматические копии перед записью")
    sp = add("restore", cmd_restore, "восстановить из резервной копии")
    sp.add_argument("file")
    sp.add_argument("--login", type=int)
    sp.add_argument("--coding-only", action="store_true", help="только длинное кодирование (0x0600)")
    sp.add_argument("--force", action="store_true", help="даже если номер детали/датасет блока другой")
    sp.add_argument("--yes", action="store_true")

    sp = add("learn", cmd_learn, "обучение: start MOD → изменить настройку → finish MOD")
    sp.add_argument("action", choices=["start", "finish"])
    sp.add_argument("module")
    sp.add_argument("--ranges", help="диапазоны DID для первичного опроса (default/full/…)")
    sp.add_argument("--name", help="имя канала (как в ODIS/VCDS, например 'Developer mode')")
    sp.add_argument("--old", help="название старого значения (или число, напр. 7.6)")
    sp.add_argument("--new", help="название нового значения (или число, напр. 12.1)")
    sp.add_argument("--did", help="какой из изменившихся DID взять")
    sp.add_argument("--byte", type=int, help="первый байт поля внутри DID (с --bit/--bits)")
    sp.add_argument("--bit", type=int, help="младший бит поля (если угадано неверно)")
    sp.add_argument("--bits", type=int, help="ширина поля в битах (если угадано неверно)")
    sp.add_argument("--recipe-name", help="сохранить все изменения как рецепт с таким названием")
    sp.add_argument("--recipe-id")
    sp.add_argument("--login", type=int, help="логин-код, нужный для записи (для рецепта)")
    sp.add_argument("--session", help="сессия, нужная для записи (например 0x4F)")

    sp = add("labels", cmd_labels, "метки каналов адаптации: list MOD | add MOD … | import-odx FILE")
    sp.add_argument("action", choices=["list", "add", "import-odx"])
    sp.add_argument("module", nargs="?")
    sp.add_argument("--name")
    sp.add_argument("--did")
    sp.add_argument("--byte", type=int, default=0)
    sp.add_argument("--bit", type=int, default=0)
    sp.add_argument("--bits", type=int, default=8)
    sp.add_argument("--values", help="'not activated=0,activated=1'")
    sp.add_argument("--session")
    sp.add_argument("--file")

    sp = add("journal", cmd_journal, "журнал записей")
    sp.add_argument("--limit", type=int, default=50)

    sp = add("web", cmd_web, "веб-интерфейс (Mac: браузер, iPhone: Pythonista)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--open", action="store_true", help="открыть браузер")

    sp = add("sim", cmd_sim, "запустить симулятор Wi-Fi адаптера с машиной (для тренировки)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=35000)
    sp.add_argument("--sfd", help="какие блоки заблокировать SFD: 5F,17")
    add("sim-reset", cmd_sim_reset, "сбросить состояние встроенного симулятора")
    sp = add("sim-change", cmd_sim_change, "(симулятор) изменить DID, как будто это сделал ODIS")
    sp.add_argument("module")
    sp.add_argument("did")
    sp.add_argument("hex")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    a = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(name)s: %(message)s")
    from .vag.vehicle import Vehicle
    Vehicle.min_write_voltage = a.min_voltage
    app = App(a.adapter, st_min_ms=a.stmin, trace=not a.no_trace)
    try:
        return a.fn(app, a) or 0
    except KeyboardInterrupt:
        out("\nПрервано.")
        return 130
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return 0
    except (VagOnError, KeyError, ValueError, IndexError) as e:
        msg = e.args[0] if isinstance(e, KeyError) and e.args else e
        out(f"Ошибка: {msg}")
        if app.trace and app.trace.path:
            out(f"(журнал обмена: {app.trace.path})")
        return 2
    finally:
        app.disconnect()


if __name__ == "__main__":
    sys.exit(main())
