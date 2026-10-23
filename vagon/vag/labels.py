"""Adaptation channel definitions ('labels'): channel name → DID + bit field + values.

VAG modules don't describe their adaptation channels; ODIS/VCDS take the
mapping from ODX datasets / label files. VagOn keeps the same information in
small JSON files so named recipes (e.g. '5F: Developer mode = activated') can
be resolved to an exact DID write on *your* module. Label files come from:

* `vagon learn` (diff before/after a change made in the MMI, ODIS or OBDeleven)
* `vagon labels import-odx file.pdx` (needs `pip install odxtools`)
* manual entry (`vagon labels add …`)

File format::

    {"vagon_labels": 1,
     "datasets": {
        "EV_MUHigh4CPRHarmaAU38X": {          # F19E of the module (prefix match)
           "module": "5F",
           "channels": [
              {"name": "Developer mode", "did": "0x0960", "byte": 0, "bit": 0, "bits": 1,
               "values": {"not activated": 0, "activated": 1}, "session": "0x4F",
               "source": "learn 2026-10-23"}
           ]}}}

`byte` = first byte of the field inside the DID data, `bit` = LSB position of
the field inside that byte, `bits` = width (may span bytes, big-endian).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field

from ..hexutil import parse_int
from ..paths import package_data, sub_dir


def norm_name(name: str) -> str:
    return re.sub(r"[\s_\-]+", " ", name.strip().lower())


@dataclass
class Channel:
    name: str
    did: int
    byte: int = 0
    bit: int = 0
    bits: int = 8
    values: dict[str, int] = field(default_factory=dict)
    session: int | None = None
    source: str = ""
    length: int | None = None        # expected DID length (bytes), optional check
    scale: float | None = None       # physical = raw * scale + offset (numeric channels)
    offset: float = 0.0
    unit: str = ""

    @classmethod
    def from_json(cls, d: dict) -> "Channel":
        values = {str(k): parse_int(v) for k, v in (d.get("values") or {}).items()}
        bits = int(d.get("bits", 8))
        if not (1 <= bits <= 64):
            raise ValueError(f"канал {d.get('name')}: неверная ширина {bits}")
        ch = cls(name=str(d["name"]), did=parse_int(d["did"], bits=16), byte=int(d.get("byte", 0)),
                 bit=int(d.get("bit", 0)), bits=bits, values=values,
                 session=parse_int(d["session"]) if d.get("session") is not None else None,
                 source=str(d.get("source", "")),
                 length=int(d["length"]) if d.get("length") is not None else None,
                 scale=float(d["scale"]) if d.get("scale") is not None else None,
                 offset=float(d.get("offset", 0.0)), unit=str(d.get("unit", "")))
        if ch.scale == 0:
            raise ValueError(f"канал {ch.name}: scale не может быть 0")
        if not (0 <= ch.bit <= 7) or ch.byte < 0:
            raise ValueError(f"канал {ch.name}: неверная позиция")
        for v in ch.values.values():
            if not (0 <= v < (1 << ch.bits)):
                raise ValueError(f"канал {ch.name}: значение {v} не помещается в {ch.bits} бит")
        return ch

    def to_json(self) -> dict:
        d = {"name": self.name, "did": f"0x{self.did:04X}", "byte": self.byte, "bit": self.bit,
             "bits": self.bits, "values": self.values}
        if self.session is not None:
            d["session"] = f"0x{self.session:02X}"
        if self.source:
            d["source"] = self.source
        if self.length is not None:
            d["length"] = self.length
        if self.scale is not None:
            d["scale"] = self.scale
            d["offset"] = self.offset
            if self.unit:
                d["unit"] = self.unit
        return d

    # ---------------------------------------------------------------- bit math
    # The field lives in data[byte : byte + n] read as one big-endian integer W,
    # n = ceil((bit + bits) / 8); value = (W >> bit) & (2**bits - 1).
    # For the common single-byte case that is simply (data[byte] >> bit) & mask.
    def _span(self, data_len: int) -> tuple[int, int]:
        nbytes = (self.bit + self.bits + 7) // 8
        if self.byte + nbytes > data_len:
            raise ValueError(f"канал {self.name}: поле выходит за длину DID ({data_len} байт)")
        return self.byte, nbytes

    def extract(self, data: bytes) -> int:
        first, n = self._span(len(data))
        word = int.from_bytes(data[first:first + n], "big")
        return (word >> self.bit) & ((1 << self.bits) - 1)

    def insert(self, data: bytes, value: int) -> bytes:
        if not (0 <= value < (1 << self.bits)):
            raise ValueError(f"значение {value} не помещается в {self.bits} бит")
        first, n = self._span(len(data))
        word = int.from_bytes(data[first:first + n], "big")
        mask = ((1 << self.bits) - 1) << self.bit
        word = (word & ~mask) | (value << self.bit)
        out = bytearray(data)
        out[first:first + n] = word.to_bytes(n, "big")
        return bytes(out)

    def value_name(self, raw: int) -> str:
        for k, v in self.values.items():
            if v == raw:
                return k
        if self.scale is not None:
            return f"{raw * self.scale + self.offset:g}{self.unit}"
        return str(raw)

    def resolve_value(self, value) -> int:
        if isinstance(value, int):
            return value
        s = str(value)
        for k, v in self.values.items():
            if norm_name(k) == norm_name(s):
                return v
        if self.scale is not None:
            try:
                phys = float(s.replace(",", ".").rstrip(self.unit or "\x00").strip())
            except ValueError:
                phys = None
            if phys is not None:
                raw = round((phys - self.offset) / self.scale)
                if not (0 <= raw < (1 << self.bits)):
                    raise ValueError(f"канал {self.name}: {value} вне диапазона")
                return raw
        try:
            return parse_int(s, bits=self.bits)
        except ValueError:
            raise ValueError(f"канал {self.name}: неизвестное значение {value!r}; допустимо: "
                             + ", ".join(self.values) ) from None


@dataclass
class Dataset:
    key: str           # F19E (or prefix)
    module: str
    channels: list[Channel]
    origin: str = ""


class LabelDB:
    def __init__(self) -> None:
        self.datasets: list[Dataset] = []
        self.load_errors: list[str] = []

    @classmethod
    def load_default(cls, extra_files: list[str] | None = None, include_user: bool = True) -> "LabelDB":
        db = cls()
        pkg = package_data("labels")
        files = []
        if os.path.isdir(pkg):
            files += [os.path.join(pkg, f) for f in sorted(os.listdir(pkg)) if f.endswith(".json")]
        if include_user:
            ud = sub_dir("labels")
            files += [os.path.join(ud, f) for f in sorted(os.listdir(ud)) if f.endswith(".json")]
        files += list(extra_files or [])
        user_root = sub_dir("labels") if include_user else None
        for f in files:
            try:
                db.load_file(f)
            except (OSError, ValueError, KeyError, TypeError) as e:
                if user_root and os.path.dirname(os.path.abspath(f)) == os.path.abspath(user_root):
                    db.load_errors.append(f"{f}: {e}")     # a broken user file must not stop VagOn
                    continue
                raise
        return db

    def load_file(self, path: str) -> None:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        self.load_dict(data, origin=path)

    def load_dict(self, data: dict, origin: str = "") -> None:
        if data.get("vagon_labels") != 1:
            raise ValueError(f"{origin}: не файл меток VagOn")
        for key, ds in (data.get("datasets") or {}).items():
            chans = [Channel.from_json(c) for c in ds.get("channels", [])]
            names = [norm_name(c.name) for c in chans]
            if len(set(names)) != len(names):
                raise ValueError(f"{origin}: повторяющиеся имена каналов в {key}")
            self.datasets.append(Dataset(key, str(ds.get("module", "")).upper(), chans, origin))

    def for_module(self, module: str, odx_dataset: str) -> list[Dataset]:
        module = module.upper()
        out = []
        for ds in self.datasets:
            if ds.module and ds.module != module:
                continue
            if ds.key == "*" or (odx_dataset and odx_dataset.startswith(ds.key)):
                out.append(ds)
        # most specific (longest key) first
        out.sort(key=lambda d: len(d.key), reverse=True)
        return out

    def find(self, module: str, odx_dataset: str, channel: str) -> Channel | None:
        want = norm_name(channel)
        for ds in self.for_module(module, odx_dataset):
            for ch in ds.channels:
                if norm_name(ch.name) == want:
                    return ch
        return None

    def channels(self, module: str, odx_dataset: str) -> list[Channel]:
        seen, out = set(), []
        for ds in self.for_module(module, odx_dataset):
            for ch in ds.channels:
                if norm_name(ch.name) not in seen:
                    seen.add(norm_name(ch.name))
                    out.append(ch)
        return out


def save_user_channel(module: str, odx_dataset: str, channel: Channel) -> str:
    """Add/replace a channel in <data>/labels/user.json (always bound to a dataset)."""
    from ..errors import VagOnError
    if not odx_dataset:
        raise VagOnError("ODX-датасет блока (F19E) не прочитан — метку не к чему привязать. Повторите.")
    channel = Channel.from_json(channel.to_json())       # validate before writing
    path = os.path.join(sub_dir("labels"), "user.json")
    data = {"vagon_labels": 1, "datasets": {}}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    ds = data["datasets"].setdefault(odx_dataset, {"module": module.upper(), "channels": []})
    if ds.get("module", "").upper() not in ("", module.upper()):
        raise VagOnError(f"датасет {odx_dataset} уже привязан к блоку {ds['module']}")
    ds["channels"] = [c for c in ds["channels"] if norm_name(c["name"]) != norm_name(channel.name)]
    ds["channels"].append(channel.to_json())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path
