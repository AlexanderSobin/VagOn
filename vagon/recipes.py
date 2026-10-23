"""Recipes: named tweaks ('Wireless CarPlay', 'Needle sweep', …) → exact writes.

A recipe lists changes on one or more modules. Change kinds:

  coding_bit   {"byte": 1, "bit": 0, "on": 1, "off": 0}            long coding (DID 0x0600)
  coding_bits  {"byte": 3, "mask": "0x0C", "on": "0x04", "off": "0x00"}
  did_bits     {"did": "0x0961", "byte": 0, "mask": "0x01", "on": 1, "off": 0}
  did_bytes    {"did": "0x0901", "offset": 0, "on": "1E", "off": "00"}
  channel      {"channel": "Developer mode", "on": "activated", "off": "not activated"}
               (resolved through label files for the module's ODX dataset)

Every change may carry "module" (default: the recipe's module). "off" may be
omitted: reverting then uses the backup VagOn saved when the recipe was applied.

Planning never writes; it re-reads the module and reports exactly which bytes
will change, plus any blocker (unknown channel, precondition mismatch, …).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from .errors import NegativeResponse, SafetyError, TimeoutError_, VagOnError
from .hexutil import parse_hex, parse_int
from .paths import package_data, sub_dir
from .vag.coding import LongCoding
from .vag.ident import DID_CODING
from .vag.labels import Channel, LabelDB, norm_name
from .vag.modules import get_module
from .vag.vehicle import Vehicle, WriteResult, did_key

KINDS = ("coding_bit", "coding_bits", "did_bits", "did_bytes", "channel")
STATUSES = {
    "tested": "проверено на реальном авто",
    "community": "из сообществ для MQB-Evo/8Y — не проверено авторами VagOn",
    "community-mqb": "из гайдов для MQB (не Evo) — на 8Y байт/бит может отличаться",
    "needs-labels": "нужны метки каналов (режим обучения или ODX)",
    "user": "ваш рецепт (записан режимом обучения на этом авто)",
}
ON_WORDS = {"on", "activated", "active", "aktiv", "aktiviert", "enabled", "enable", "yes", "ein",
            "installed", "verbaut", "1", "true", "вкл"}
OFF_WORDS = {"off", "not activated", "inactive", "nicht aktiv", "deaktiviert", "disabled", "disable",
             "no", "aus", "not installed", "nicht verbaut", "0", "false", "выкл"}


def _t(d, lang="ru") -> str:
    if isinstance(d, dict):
        return d.get(lang) or d.get("ru") or d.get("en") or ""
    return str(d or "")


@dataclass
class Change:
    kind: str
    module: str
    spec: dict

    def describe(self, lang: str = "ru") -> str:
        s = self.spec
        if self.kind == "coding_bit":
            return f"{self.module}: кодирование Byte {s['byte']} Bit {s['bit']} = {s.get('on')}"
        if self.kind == "coding_bits":
            return f"{self.module}: кодирование Byte {s['byte']} маска {s['mask']} = {s.get('on')}"
        if self.kind == "did_bits":
            return f"{self.module}: DID {s['did']} байт {s['byte']} маска {s['mask']} = {s.get('on')}"
        if self.kind == "did_bytes":
            return f"{self.module}: DID {s['did']} @{s.get('offset', 0)} = {s.get('on')}"
        return f"{self.module}: адаптация «{s['channel']}» = {s.get('on')}"


@dataclass
class Recipe:
    id: str
    title: dict
    module: str
    changes: list[Change]
    description: dict = field(default_factory=dict)
    category: str = "other"
    status: str = "community"
    logins: dict[str, int] = field(default_factory=dict)
    sessions: dict[str, int] = field(default_factory=dict)
    requires: dict = field(default_factory=dict)
    sfd: str = "unknown"
    sources: list[str] = field(default_factory=list)
    notes: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)     # what to do after applying (e.g. reboot MMI)
    cars: list[str] = field(default_factory=list)
    origin: str = ""

    @property
    def modules(self) -> list[str]:
        seen = []
        for c in self.changes:
            if c.module not in seen:
                seen.append(c.module)
        return seen

    def to_summary(self, lang: str = "ru") -> dict:
        return {"id": self.id, "title": _t(self.title, lang), "description": _t(self.description, lang),
                "category": self.category, "status": self.status,
                "status_text": STATUSES.get(self.status, self.status), "modules": self.modules,
                "sfd": self.sfd, "notes": _t(self.notes, lang), "after": _t(self.after, lang),
                "sources": self.sources, "changes": [c.describe(lang) for c in self.changes],
                "reversible": all(("off" in c.spec) for c in self.changes)}


def _norm_module(m) -> str:
    return get_module(m).addr_hex


def recipe_from_json(d: dict, origin: str = "") -> Recipe:
    rid = str(d["id"])
    module = _norm_module(d["module"])
    changes = []
    for i, c in enumerate(d.get("changes", [])):
        kind = c.get("kind")
        if kind not in KINDS:
            raise ValueError(f"{rid}: изменение #{i}: неизвестный тип {kind!r}")
        mod = _norm_module(c.get("module", module))
        spec = {k: v for k, v in c.items() if k not in ("kind", "module")}
        if kind in ("coding_bit", "coding_bits", "did_bits") and "byte" not in spec:
            raise ValueError(f"{rid}: изменение #{i}: нет 'byte'")
        if kind in ("did_bits", "did_bytes") and "did" not in spec:
            raise ValueError(f"{rid}: изменение #{i}: нет 'did'")
        if kind == "channel" and not spec.get("channel"):
            raise ValueError(f"{rid}: изменение #{i}: нет 'channel'")
        if "on" not in spec:
            raise ValueError(f"{rid}: изменение #{i}: нет значения 'on'")
        changes.append(Change(kind, mod, spec))
    if not changes:
        raise ValueError(f"{rid}: нет изменений")
    logins = {}
    raw_login = d.get("login")
    if isinstance(raw_login, (int, str)) and str(raw_login).strip():
        logins[module] = parse_int(raw_login)
    elif isinstance(raw_login, dict):
        logins = {_norm_module(k): parse_int(v) for k, v in raw_login.items()}
    sessions = {}
    raw_sess = d.get("session")
    if isinstance(raw_sess, (int, str)) and str(raw_sess).strip():
        sessions[module] = parse_int(raw_sess)
    elif isinstance(raw_sess, dict):
        sessions = {_norm_module(k): parse_int(v) for k, v in raw_sess.items()}
    status = d.get("status", "community")
    if status not in STATUSES:
        raise ValueError(f"{rid}: неизвестный статус {status!r}")
    return Recipe(id=rid, title=d.get("title", {"ru": rid}), module=module, changes=changes,
                  description=d.get("description", {}), category=d.get("category", "other"),
                  status=status, logins=logins, sessions=sessions, requires=d.get("requires", {}),
                  sfd=d.get("sfd", "unknown"), sources=list(d.get("sources", [])),
                  notes=d.get("notes", {}), after=d.get("after", {}), cars=list(d.get("cars", [])),
                  origin=origin)


def recipe_to_json(r: Recipe) -> dict:
    d = {"id": r.id, "title": r.title, "description": r.description, "category": r.category,
         "module": r.module, "status": r.status, "sfd": r.sfd, "cars": r.cars,
         "changes": [dict(kind=c.kind, **({"module": c.module} if c.module != r.module else {}), **c.spec)
                     for c in r.changes],
         "sources": r.sources, "notes": r.notes, "after": r.after}
    if r.logins:
        d["login"] = {k: v for k, v in r.logins.items()}
    if r.sessions:
        d["session"] = {k: f"0x{v:02X}" for k, v in r.sessions.items()}
    if r.requires:
        d["requires"] = r.requires
    return d


class RecipeBook:
    def __init__(self) -> None:
        self.recipes: dict[str, Recipe] = {}

    @classmethod
    def load_default(cls, include_user: bool = True, extra_dirs: list[str] | None = None) -> "RecipeBook":
        book = cls()
        dirs = [package_data("recipes")]
        if include_user:
            dirs.append(sub_dir("recipes"))
        dirs += list(extra_dirs or [])
        for d in dirs:
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if name.endswith(".json"):
                    book.load_file(os.path.join(d, name))
        return book

    def load_file(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("recipes", []) if isinstance(data, dict) else data
        for item in items:
            r = recipe_from_json(item, origin=path)
            if r.id in self.recipes and self.recipes[r.id].origin != path:
                # user recipes override bundled ones with the same id
                pass
            self.recipes[r.id] = r

    def get(self, rid: str) -> Recipe:
        try:
            return self.recipes[rid]
        except KeyError:
            matches = [k for k in self.recipes if rid.lower() in k.lower()]
            if len(matches) == 1:
                return self.recipes[matches[0]]
            raise KeyError(f"нет рецепта {rid!r}" + (f"; похожие: {', '.join(matches)}" if matches else ""))

    def list(self, category: str | None = None) -> list[Recipe]:
        out = [r for r in self.recipes.values() if not category or r.category == category]
        return sorted(out, key=lambda r: (r.category, r.id))


# ---------------------------------------------------------------------- planning
@dataclass
class ModulePlan:
    module: str
    writes: dict[int, tuple[bytes, bytes]] = field(default_factory=dict)   # did → (old, new)
    lines: list[str] = field(default_factory=list)
    login: int | None = None
    session: int = 0x03
    ident: dict = field(default_factory=dict)


@dataclass
class Plan:
    recipe: Recipe
    action: str                                  # "on" | "off"
    modules: list[ModulePlan] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    state: str = "unknown"                       # on / off / partial / unknown

    @property
    def nothing_to_do(self) -> bool:
        return not self.blockers and all(not m.writes for m in self.modules)

    def to_dict(self) -> dict:
        return {
            "recipe": self.recipe.id, "action": self.action, "state": self.state,
            "blockers": self.blockers, "warnings": self.warnings,
            "nothing_to_do": self.nothing_to_do,
            "modules": [{"module": m.module, "login": m.login, "session": f"0x{m.session:02X}",
                         "lines": m.lines,
                         "writes": {did_key(d): [o.hex().upper(), n.hex().upper()]
                                    for d, (o, n) in m.writes.items()}} for m in self.modules],
        }


def _pick_value(ch: Channel, wanted, side: str):
    """Map 'activated'/'ON'/… to the label's own value names."""
    try:
        return ch.resolve_value(wanted)
    except ValueError:
        pass
    words = ON_WORDS if side == "on" else OFF_WORDS
    if norm_name(str(wanted)) in words:
        cands = [v for k, v in ch.values.items() if norm_name(k) in words]
        if len(cands) == 1:
            return cands[0]
    raise ValueError(f"канал «{ch.name}»: значение {wanted!r} не найдено среди {list(ch.values)}")


def _check_requires(recipe: Recipe, module: str, ident: dict, coding_len: int | None) -> list[str]:
    req = recipe.requires.get(module) if isinstance(recipe.requires.get(module), dict) else (
        recipe.requires if module == recipe.module else {})
    problems = []
    ds = ident.get("odx_dataset", "")
    part = ident.get("part_number", "")
    sw = ident.get("sw_version", "")
    if req.get("dataset_prefix") and not any(ds.startswith(p) for p in req["dataset_prefix"]):
        problems.append(f"{module}: датасет {ds or '?'} не из списка {req['dataset_prefix']}")
    if req.get("part_prefix") and not any(part.startswith(p) for p in req["part_prefix"]):
        problems.append(f"{module}: номер детали {part or '?'} не из списка {req['part_prefix']}")
    if req.get("sw_not") and any(sw.startswith(s) for s in req["sw_not"]):
        problems.append(f"{module}: версия ПО {sw} в списке известных несовместимых {req['sw_not']}")
    if req.get("coding_length") and coding_len is not None and coding_len != int(req["coding_length"]):
        problems.append(f"{module}: длина кодировки {coding_len}, рецепт рассчитан на {req['coding_length']}")
    return problems


def _latest_recipe_backup(vehicle: Vehicle, recipe: Recipe, module: str):
    tag = f"recipe {recipe.id} on"
    found = [b for b in vehicle.store.list_backups(vin=vehicle.vin, module=module)
             if b.data.get("reason", "") == tag]
    return found[-1] if found else None


def plan_recipe(vehicle: Vehicle, recipe: Recipe, action: str = "on",
                labels: LabelDB | None = None) -> Plan:
    if action not in ("on", "off"):
        raise ValueError("action: on | off")
    labels = labels or LabelDB.load_default()
    plan = Plan(recipe, action)
    states: list[bool | None] = []
    for mod in recipe.modules:
        mp = ModulePlan(mod, login=recipe.logins.get(mod), session=recipe.sessions.get(mod, 0x03))
        plan.modules.append(mp)
        sess = vehicle.module(mod)
        if sess.module.protected:
            plan.blockers.append(f"{mod}: защищённый блок — VagOn в него не пишет")
            continue
        try:
            ident = sess.ident(refresh=True)
        except TimeoutError_:
            plan.blockers.append(f"{mod}: блок не отвечает (нет в этой машине?)")
            continue
        mp.ident = ident.to_dict()
        cur_values: dict[int, bytes] = {}
        new_values: dict[int, bytearray] = {}

        def current(did: int) -> bytes:
            if did not in cur_values:
                cur_values[did] = sess.read_did(did)
                new_values[did] = bytearray(cur_values[did])
            return cur_values[did]

        coding_len = None
        if ident.coding:
            coding_len = len(ident.coding) // 2
        plan.blockers += _check_requires(recipe, mod, mp.ident, coding_len)
        backup = None
        for ch_ in [c for c in recipe.changes if c.module == mod]:
            s = ch_.spec
            want = s.get(action)
            try:
                if want is None:
                    if action == "off":
                        backup = backup or _latest_recipe_backup(vehicle, recipe, mod)
                        if backup is None:
                            plan.blockers.append(f"{ch_.describe()}: нет значения 'off' и нет резервной "
                                                 "копии от включения — используйте restore")
                            continue
                    else:
                        continue
                if ch_.kind in ("coding_bit", "coding_bits"):
                    did = DID_CODING
                    try:
                        cur = current(did)
                    except NegativeResponse:
                        plan.blockers.append(f"{mod}: у блока нет длинного кодирования")
                        continue
                    lc = LongCoding(bytes(new_values[did]))
                    byte = int(s["byte"])
                    if byte >= len(lc):
                        plan.blockers.append(f"{mod}: Byte {byte} отсутствует (кодировка {len(lc)} байт)")
                        continue
                    if ch_.kind == "coding_bit":
                        bit = int(s["bit"])
                        if want is None:
                            want = (bytes.fromhex(backup.dids[did_key(did)])[byte] >> bit) & 1
                        old_bit = (cur[byte] >> bit) & 1
                        lc.set_bit(byte, bit, int(want))
                        states.append(old_bit == int(s["on"]))
                        mp.lines.append(f"Byte {byte} Bit {bit}: {old_bit} → {int(want)}")
                    else:
                        mask = parse_int(s["mask"], bits=8)
                        if want is None:
                            want = bytes.fromhex(backup.dids[did_key(did)])[byte] & mask
                        val = parse_int(want, bits=8)
                        old = cur[byte] & mask
                        lc.set_field(byte, mask, val)
                        states.append(old == parse_int(s["on"], bits=8))
                        mp.lines.append(f"Byte {byte} & {mask:#04x}: {old:#04x} → {val:#04x}")
                    new_values[did] = bytearray(bytes(lc))
                elif ch_.kind == "did_bits":
                    did = parse_int(s["did"], bits=16)
                    cur = current(did)
                    byte = int(s["byte"])
                    if byte >= len(cur):
                        plan.blockers.append(f"{mod}: DID {did_key(did)} короче {byte + 1} байт")
                        continue
                    mask = parse_int(s["mask"], bits=8)
                    if want is None:
                        want = bytes.fromhex(backup.dids[did_key(did)])[byte] & mask
                    val = parse_int(want, bits=8)
                    if val & ~mask & 0xFF:
                        raise ValueError(f"значение {val:#04x} не помещается в маску {mask:#04x}")
                    old = cur[byte] & mask
                    new_values[did][byte] = (new_values[did][byte] & ~mask & 0xFF) | val
                    states.append(old == parse_int(s["on"], bits=8))
                    mp.lines.append(f"DID {did_key(did)} байт {byte} & {mask:#04x}: {old:#04x} → {val:#04x}")
                elif ch_.kind == "did_bytes":
                    did = parse_int(s["did"], bits=16)
                    cur = current(did)
                    off = int(s.get("offset", 0))
                    if want is None:
                        n = len(parse_hex(s["on"]))
                        raw = bytes.fromhex(backup.dids[did_key(did)])[off:off + n]
                    else:
                        raw = parse_hex(want)
                    if off + len(raw) > len(cur):
                        plan.blockers.append(f"{mod}: DID {did_key(did)} короче, чем нужно рецепту")
                        continue
                    old = cur[off:off + len(raw)]
                    new_values[did][off:off + len(raw)] = raw
                    states.append(old == parse_hex(s["on"]))
                    mp.lines.append(f"DID {did_key(did)} [{off}..{off + len(raw) - 1}]: "
                                    f"{old.hex().upper()} → {raw.hex().upper()}")
                elif ch_.kind == "channel":
                    chan = None
                    for name in [s["channel"]] + list(s.get("alt", [])):
                        chan = labels.find(mod, ident.odx_dataset, name)
                        if chan is not None:
                            break
                    if chan is None:
                        msg = (f"{mod}: канал «{s['channel']}» неизвестен для датасета "
                               f"{ident.odx_dataset or '?'} — выучите его (vagon learn) или импортируйте ODX")
                        if s.get("optional"):
                            plan.warnings.append(msg + " (необязательный — пропущен)")
                        else:
                            plan.blockers.append(msg)
                        continue
                    if chan.session is not None and mp.session == 0x03:
                        mp.session = chan.session
                    cur = current(chan.did)
                    if chan.length is not None and chan.length != len(cur):
                        plan.blockers.append(f"{mod}: длина DID {did_key(chan.did)} изменилась "
                                             f"({len(cur)} вместо {chan.length}) — метка устарела")
                        continue
                    if want is None:
                        val = chan.extract(bytes.fromhex(backup.dids[did_key(chan.did)]))
                    else:
                        val = _pick_value(chan, want, action)
                    old = chan.extract(cur)
                    new_values[chan.did][:] = chan.insert(bytes(new_values[chan.did]), val)
                    on_val = _pick_value(chan, s["on"], "on")
                    states.append(old == on_val)
                    mp.lines.append(f"«{chan.name}»: {chan.value_name(old)} → {chan.value_name(val)} "
                                    f"(DID {did_key(chan.did)})")
            except (NegativeResponse, ValueError, KeyError, IndexError) as e:
                plan.blockers.append(f"{ch_.describe()}: {e}")
        for did, new in new_values.items():
            if bytes(new) != cur_values[did]:
                mp.writes[did] = (cur_values[did], bytes(new))
    if states and all(s is True for s in states):
        plan.state = "on"
    elif states and all(s is False for s in states):
        plan.state = "off"
    elif states:
        plan.state = "partial"
    if recipe.status in ("community-mqb",):
        plan.warnings.append("Байты/биты взяты из гайдов для MQB (не Evo). На 8Y раскладка может отличаться. "
                             "Сверьте с описанием в ODIS/VCDS, если есть возможность.")
    if recipe.sfd in ("yes", "likely"):
        plan.warnings.append("На 8Y этот блок обычно защищён SFD: запись пройдёт только во время легальной "
                             "разблокировки SFD (OBDeleven/VCDS/ODIS онлайн).")
    return plan


def apply_plan(vehicle: Vehicle, plan: Plan, *, progress=None, force_login: bool = False) -> list[WriteResult]:
    if plan.blockers:
        raise SafetyError("план не выполним:\n" + "\n".join(plan.blockers))
    results = []
    reason = f"recipe {plan.recipe.id} {plan.action}"
    for mp in plan.modules:
        if not mp.writes:
            continue
        sess = vehicle.module(mp.module)
        changes = {did: new for did, (_old, new) in mp.writes.items()}
        try:
            results.append(sess.write(changes, reason=reason, login=mp.login, session=mp.session,
                                      progress=progress, force_login=force_login))
        except VagOnError as e:
            done = ", ".join(r.module for r in results)
            raise VagOnError(f"{e}\n(блоки, уже изменённые этим рецептом: {done or 'нет'}; "
                             "для отката: restore из резервных копий)") from e
    return results


# ---------------------------------------------------------------------- learned recipes
def build_learned_recipe(rid: str, title: str, module: str, changes, ident, *,
                         login: int | None = None, session: int | None = None) -> Recipe:
    """Turn learn-mode DID changes into a minimal did_bits recipe (only flipped bits)."""
    import time as _time
    rch = []
    for c in changes:
        for byte in sorted({b for b, _ in c.changed_bits()}):
            mask = c.old[byte] ^ c.new[byte]
            rch.append(Change("did_bits", module, {
                "did": did_key(c.did), "byte": byte, "mask": f"0x{mask:02X}",
                "on": f"0x{c.new[byte] & mask:02X}", "off": f"0x{c.old[byte] & mask:02X}"}))
    if not rch:
        raise VagOnError("нет изменений для рецепта")
    rec = Recipe(id=rid, title={"ru": title, "en": title}, module=module, changes=rch,
                 category="learned", status="user", sfd="unknown",
                 description={"ru": f"Записано режимом обучения {_time.strftime('%Y-%m-%d')} на "
                                    f"{ident.part_number} SW {ident.sw_version}"},
                 requires={"dataset_prefix": [ident.odx_dataset]} if ident.odx_dataset else {})
    if login is not None:
        rec.logins[module] = login
    if session is not None:
        rec.sessions[module] = session
    return rec


def save_user_recipe(recipe: Recipe, filename: str = "learned.json") -> str:
    path = os.path.join(sub_dir("recipes"), filename)
    data = {"vagon_recipes": 1, "recipes": []}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    data["recipes"] = [x for x in data.get("recipes", []) if x.get("id") != recipe.id]
    data["recipes"].append(recipe_to_json(recipe))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path
