"""Learn mode: discover which DID/bits a change touches by diffing snapshots.

Typical use (once per tweak):
  1. `vagon learn start 5F`   – reads every known DID of the module (twice, to
     drop live values that change on their own)
  2. make the change with the MMI menu, ODIS or OBDeleven
  3. `vagon learn finish 5F --name "Developer mode" --old "not activated"
     --new "activated"` – reads again, diffs, saves a label (channel) and a
     recipe you can apply/revert from VagOn afterwards (Mac or iPhone).

DID discovery (`sweep`) asks the module for every identifier in the given
ranges (read-only, safe) and remembers which ones exist.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable

from ..errors import NegativeResponse, TimeoutError_, VagOnError
from ..paths import sub_dir
from .labels import Channel
from .vehicle import ModuleSession

# Where VAG keeps adaptation DIDs is not publicly documented, so the default discovery covers
# 0x0000-0x3FFF (≈16 k reads, one-off and resumable); 'full' covers everything but F2xx-FCxx.
DEFAULT_RANGES = [(0x0000, 0x3FFF)]
FULL_RANGES = [(0x0000, 0xEFFF), (0xF100, 0xF1FF), (0xFD00, 0xFEFF)]


def parse_ranges(text: str | None) -> list[tuple[int, int]]:
    if not text or text == "default":
        return list(DEFAULT_RANGES)
    if text == "full":
        return list(FULL_RANGES)
    out = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            lo, hi = int(a, 16), int(b, 16)
        else:
            lo = hi = int(part, 16)
        if not (0 <= lo <= hi <= 0xFFFF):
            raise ValueError(f"неверный диапазон {part}")
        out.append((lo, hi))
    return out


def _sweep_file(vin: str, module: str, dataset: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in f"{vin}_{module}_{dataset}")[:100]
    return os.path.join(sub_dir("dids"), safe + ".json")


def load_known_dids(vin: str, module: str, dataset: str) -> dict:
    path = _sweep_file(vin, module, dataset)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {"module": module, "dataset": dataset, "vin": vin, "done": [], "dids": {}}


def save_known_dids(vin: str, module: str, dataset: str, data: dict) -> str:
    path = _sweep_file(vin, module, dataset)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)
    return path


def sweep(sess: ModuleSession, ranges: list[tuple[int, int]], *, vin: str,
          progress: Callable[[int, int, int], None] | None = None,
          should_stop: Callable[[], bool] | None = None) -> dict:
    """Probe DIDs; resumable (progress saved every 64 DIDs). Returns known-DID data."""
    ident = sess.ident()
    data = load_known_dids(vin, sess.module.addr_hex, ident.odx_dataset)
    done = set(data.get("done", []))
    todo = [d for lo, hi in ranges for d in range(lo, hi + 1) if d not in done]
    fast = getattr(sess.vehicle.transport, "fast_timeouts", None)
    if fast and todo:
        fast(True)       # absent DIDs answer NRC 0x31 quickly; don't wait ~1 s after each answer
    try:
        _sweep_loop(sess, todo, done, data, vin, ident, progress, should_stop)
    finally:
        if fast and todo:
            fast(False)
    data["done"] = sorted(done)
    save_known_dids(vin, sess.module.addr_hex, ident.odx_dataset, data)
    return data


def _sweep_loop(sess, todo, done, data, vin, ident, progress, should_stop) -> None:
    total = len(todo)
    for i, did in enumerate(todo):
        if should_stop and should_stop():
            break
        try:
            value = sess.read_did(did)
            data["dids"][f"0x{did:04X}"] = value.hex().upper()
        except NegativeResponse:
            pass
        except TimeoutError_:
            # one retry: a lost frame shouldn't hide a DID
            try:
                value = sess.read_did(did)
                data["dids"][f"0x{did:04X}"] = value.hex().upper()
            except (NegativeResponse, TimeoutError_):
                pass
        done.add(did)
        if i % 64 == 63 or i == total - 1:
            data["done"] = sorted(done)
            save_known_dids(vin, sess.module.addr_hex, ident.odx_dataset, data)
        if progress:
            progress(i + 1, total, len(data["dids"]))


def snapshot(sess: ModuleSession, dids: list[int]) -> dict[int, bytes]:
    out: dict[int, bytes] = {}
    for did in dids:
        try:
            out[did] = sess.read_did(did)
        except (NegativeResponse, TimeoutError_):
            continue
    return out


@dataclass
class DidChange:
    did: int
    old: bytes
    new: bytes

    def changed_bits(self) -> list[tuple[int, int]]:
        """[(byte index, bit)] that differ."""
        out = []
        for i, (a, b) in enumerate(zip(self.old, self.new)):
            x = a ^ b
            out += [(i, bit) for bit in range(8) if x >> bit & 1]
        return out


# Identification / workshop data that every tester rewrites (F198 tester code, F199 date,
# F1A5 fingerprint…) — never part of a learned setting.
IGNORED_DIDS = set(range(0xF180, 0xF1FF + 1))


def diff(before: dict[int, bytes], after: dict[int, bytes], noisy: set[int] | None = None) -> list[DidChange]:
    out = []
    for did in sorted(set(before) & set(after)):
        if (noisy and did in noisy) or did in IGNORED_DIDS:
            continue
        if before[did] != after[did] and len(before[did]) == len(after[did]):
            out.append(DidChange(did, before[did], after[did]))
    return out


def infer_channel(change: DidChange, name: str, old_name: str = "", new_name: str = "",
                  source: str = "", bit: int | None = None, bits: int | None = None,
                  byte: int | None = None) -> Channel:
    """Bit field for one learned change.

    * named values ("active" → "not active"): the smallest field covering the flipped bits,
      which must lie in ONE byte (otherwise save the change as a recipe instead);
    * numbers that equal the raw values of that smallest field ("1" → "0") stay a flag;
    * other numbers (10 s → 30 s, 7.6 V → 12.1 V) cover whole bytes, scale/offset derived;
    * `bit` + `bits` (and optionally `byte`) set the field explicitly; it must cover every
      flipped bit.
    """
    changed = change.changed_bits()
    if not changed:
        raise VagOnError("в этом DID ничего не изменилось")
    first_byte = min(b for b, _ in changed)
    last_byte = max(b for b, _ in changed)
    old_f, new_f = _as_float(old_name), _as_float(new_name)
    numeric = old_f is not None and new_f is not None and old_f != new_f
    length = len(change.old)

    def channel(b0: int, lo: int, width: int) -> Channel:
        return Channel(name=name, did=change.did, byte=b0, bit=lo, bits=width, source=source, length=length)

    if bit is not None or bits is not None or byte is not None:
        if bit is None or bits is None:
            raise VagOnError("укажите поле целиком: --bit и --bits (и при необходимости --byte)")
        ch = channel(first_byte if byte is None else byte, bit, bits)
        _require_cover(ch, changed)
    else:
        positions = [(last_byte - b) * 8 + bt for b, bt in changed]
        lo, hi = min(positions), max(positions)
        minimal = channel(first_byte, lo, hi - lo + 1)
        if numeric and not (old_f == minimal.extract(change.old) and new_f == minimal.extract(change.new)):
            ch = channel(first_byte, 0, (last_byte - first_byte + 1) * 8)     # a real number: whole bytes
        else:
            numeric = False
            if first_byte != last_byte:
                raise VagOnError("изменились биты в нескольких байтах — это не похоже на один канал. "
                                 "Сохраните изменение рецептом (--recipe-name) или задайте поле явно "
                                 "(--byte/--bit/--bits).")
            ch = minimal
    old_v, new_v = ch.extract(change.old), ch.extract(change.new)
    if old_v == new_v:
        raise VagOnError("поле не захватывает изменившиеся биты — проверьте --bit/--bits")
    if numeric:
        ch.scale = (new_f - old_f) / (new_v - old_v)
        ch.offset = old_f - old_v * ch.scale
    else:
        ch.values = {(old_name or "old"): old_v, (new_name or "new"): new_v}
        if (old_name or "old") == (new_name or "new"):
            raise VagOnError("старое и новое значение названы одинаково")
    Channel.from_json(ch.to_json())        # validates the field before it is ever saved
    return ch


def _require_cover(ch: Channel, changed: list[tuple[int, int]]) -> None:
    n = (ch.bit + ch.bits + 7) // 8
    for b, bt in changed:
        if not (ch.byte <= b < ch.byte + n):
            raise VagOnError(f"байт {b} изменился, но не входит в заданное поле")
        pos = (ch.byte + n - 1 - b) * 8 + bt
        if not (ch.bit <= pos < ch.bit + ch.bits):
            raise VagOnError(f"бит {bt} байта {b} изменился, но не входит в заданное поле")


def _as_float(text: str) -> float | None:
    try:
        return float(str(text).replace(",", ".")) if text not in (None, "") else None
    except ValueError:
        return None


class LearnSession:
    """Persisted between 'start' and 'finish' (they may be minutes/days apart)."""

    def __init__(self, path: str, data: dict):
        self.path = path
        self.data = data

    @classmethod
    def start(cls, sess: ModuleSession, dids: list[int], vin: str) -> "LearnSession":
        ident = sess.ident(refresh=True)
        first = snapshot(sess, dids)
        time.sleep(0.5)
        second = snapshot(sess, list(first))
        noisy = [d for d in first if second.get(d) != first[d]]
        data = {
            "module": sess.module.addr_hex, "vin": vin, "dataset": ident.odx_dataset,
            "part": ident.part_number, "sw": ident.sw_version,
            "started": time.strftime("%Y-%m-%d %H:%M:%S"),
            "before": {f"0x{d:04X}": v.hex().upper() for d, v in second.items()},
            "noisy": [f"0x{d:04X}" for d in noisy],
        }
        path = os.path.join(sub_dir("learn"), f"{sess.module.addr_hex}_pending.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        return cls(path, data)

    @classmethod
    def load(cls, module: str) -> "LearnSession":
        path = os.path.join(sub_dir("learn"), f"{module.upper()}_pending.json")
        if not os.path.exists(path):
            raise VagOnError(f"нет начатого обучения для блока {module}: сначала 'learn start {module}'")
        with open(path, encoding="utf-8") as f:
            return cls(path, json.load(f))

    def before(self) -> dict[int, bytes]:
        return {int(k, 16): bytes.fromhex(v) for k, v in self.data["before"].items()}

    def noisy(self) -> set[int]:
        return {int(k, 16) for k in self.data.get("noisy", [])}

    def finish(self, sess: ModuleSession) -> list[DidChange]:
        """Second snapshot + diff. The 'before' snapshot is kept until commit(), so a
        rejected label (wrong name/field) can be retried without starting over."""
        before = self.before()
        after = snapshot(sess, list(before))
        self.data["after"] = {f"0x{d:04X}": v.hex().upper() for d, v in after.items()}
        return diff(before, after, self.noisy())

    def commit(self) -> str:
        """Archive the finished learn session (removes the pending snapshot)."""
        done = self.path.replace("_pending.json", f"_{time.strftime('%Y%m%d_%H%M%S')}.json")
        with open(done, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        if os.path.exists(self.path):
            os.remove(self.path)
        return done
