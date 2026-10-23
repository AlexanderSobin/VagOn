"""Backups (before every write) and the write journal."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass

from ..paths import sub_dir


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H-%M-%S")


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:60] or "x"


@dataclass
class Backup:
    path: str
    data: dict

    @property
    def module(self) -> str:
        return self.data.get("module", "??")

    @property
    def created(self) -> str:
        return self.data.get("created", "")

    @property
    def coding(self) -> str | None:
        return self.data.get("coding")

    @property
    def dids(self) -> dict[str, str]:
        return self.data.get("dids", {})


class Store:
    """Files live in <data_dir>/backups/<VIN>/ and <data_dir>/journal.jsonl."""

    def __init__(self, root: str | None = None):
        self.root = root

    def _dir(self, *parts: str) -> str:
        if self.root:
            path = os.path.join(self.root, *parts)
            os.makedirs(path, exist_ok=True)
            return path
        return sub_dir(*parts)

    # ---------------------------------------------------------------- backups
    def save_backup(self, *, vin: str, module: str, ident: dict, coding: str | None,
                    dids: dict[str, str], reason: str) -> Backup:
        data = {
            "vagon_backup": 1,
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
            "vin": vin,
            "module": module,
            "ident": ident,
            "coding": coding,
            "dids": dids,
            "reason": reason,
        }
        folder = self._dir("backups", _safe(vin or "UNKNOWN_VIN"))
        base = f"{_now()}_{module}_{_safe(reason)[:30]}"
        path = os.path.join(folder, base + ".json")
        n = 1
        while os.path.exists(path):
            n += 1
            path = os.path.join(folder, f"{base}_{n}.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return Backup(path, data)

    def list_backups(self, vin: str | None = None, module: str | None = None) -> list[Backup]:
        root = self._dir("backups")
        out = []
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                if not name.endswith(".json"):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                except (OSError, ValueError):
                    continue
                if data.get("vagon_backup") != 1:
                    continue
                if vin and data.get("vin") != vin:
                    continue
                if module and data.get("module", "").upper() != module.upper():
                    continue
                out.append(Backup(path, data))
        out.sort(key=lambda b: (b.created, b.path))
        return out

    @staticmethod
    def load_backup(path: str) -> Backup:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("vagon_backup") != 1:
            raise ValueError(f"{path} — не резервная копия VagOn")
        return Backup(path, data)

    # ---------------------------------------------------------------- journal
    def journal(self, **entry) -> None:
        entry.setdefault("time", time.strftime("%Y-%m-%d %H:%M:%S"))
        path = os.path.join(self._dir(), "journal.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_journal(self, limit: int = 200) -> list[dict]:
        path = os.path.join(self._dir(), "journal.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        out = []
        for ln in lines:
            try:
                out.append(json.loads(ln))
            except ValueError:
                continue
        return out

    # ---------------------------------------------------------------- generic
    def save_json(self, folder: str, name: str, data: dict) -> str:
        path = os.path.join(self._dir(folder), _safe(name) + ".json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path

    def user_dir(self, folder: str) -> str:
        return self._dir(folder)
