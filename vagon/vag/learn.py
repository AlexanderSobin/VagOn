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

DEFAULT_RANGES = [(0x0000, 0x0FFF), (0x2000, 0x3FFF)]
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
    data["done"] = sorted(done)
    save_known_dids(vin, sess.module.addr_hex, ident.odx_dataset, data)
    return data


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
                  source: str = "", bit: int | None = None, bits: int | None = None) -> Channel:
    """Bit field for one learned change.

    Numeric values (old/new given as numbers) cover whole bytes, because one change
    reveals only the bits that happened to flip. Named values get the smallest field
    covering every changed bit; `bit`/`bits` override the guess.
    """
    changed = change.changed_bits()
    if not changed:
        raise VagOnError("в этом DID ничего не изменилось")
    first_byte = min(b for b, _ in changed)
    last_byte = max(b for b, _ in changed)
    numeric = _as_float(old_name) is not None and _as_float(new_name) is not None
    if numeric:
        lo, width = 0, (last_byte - first_byte + 1) * 8
    else:
        # bit positions inside the big-endian word data[first_byte:last_byte+1]
        positions = [(last_byte - b) * 8 + bt for b, bt in changed]
        lo, hi = min(positions), max(positions)
        width = hi - lo + 1
    if bit is not None:
        lo = bit
    if bits is not None:
        width = bits
    ch = Channel(name=name, did=change.did, byte=first_byte, bit=lo, bits=width, source=source,
                 length=len(change.old))
    old_v, new_v = ch.extract(change.old), ch.extract(change.new)
    ch.values = {}
    old_f, new_f = _as_float(old_name), _as_float(new_name)
    if old_f is not None and new_f is not None and old_f != new_f:
        # two physical points → linear scaling (e.g. 7.6 V → 12.1 V)
        ch.scale = (new_f - old_f) / (new_v - old_v)
        ch.offset = old_f - old_v * ch.scale
        return ch
    if old_name:
        ch.values[old_name] = old_v
    if new_name:
        ch.values[new_name] = new_v
    if not ch.values:
        ch.values = {"old": old_v, "new": new_v}
    return ch


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
        before = self.before()
        after = snapshot(sess, list(before))
        changes = diff(before, after, self.noisy())
        done = self.path.replace("_pending.json", f"_{time.strftime('%Y%m%d_%H%M%S')}.json")
        self.data["after"] = {f"0x{d:04X}": v.hex().upper() for d, v in after.items()}
        with open(done, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=1)
        os.remove(self.path)
        return changes
