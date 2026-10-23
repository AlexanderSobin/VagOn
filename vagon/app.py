"""Application context shared by the CLI and the web UI."""

from __future__ import annotations

import os
import threading
import time

from .connect import default_adapter, is_sim, open_adapter
from .paths import data_dir, sub_dir
from .recipes import RecipeBook
from .transport.elm327 import Elm327
from .vag.labels import LabelDB
from .vag.store import Store
from .vag.vehicle import Vehicle

SIM_LABELS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim", "labels_sim.json")
KEEP_LOGS = 30


class TraceLog:
    """Every line exchanged with the adapter, for post-mortem diagnostics."""

    def __init__(self, path: str | None):
        self.path = path
        self._f = open(path, "a", encoding="utf-8") if path else None
        self._lock = threading.Lock()
        self._t0 = time.monotonic()

    def __call__(self, direction: str, text: str) -> None:
        if not self._f:
            return
        with self._lock:
            self._f.write(f"{time.monotonic() - self._t0:9.3f} {direction} {text}\n")
            self._f.flush()

    def close(self) -> None:
        if self._f:
            self._f.close()
            self._f = None


def _rotate_logs(folder: str) -> None:
    logs = sorted(f for f in os.listdir(folder) if f.startswith("session_") and f.endswith(".log"))
    for old in logs[:-KEEP_LOGS]:
        try:
            os.remove(os.path.join(folder, old))
        except OSError:
            pass


class App:
    def __init__(self, adapter: str | None = None, *, st_min_ms: int = 10, trace: bool = True,
                 store: Store | None = None):
        self.adapter_url = adapter or default_adapter()
        self.st_min_ms = st_min_ms
        self.trace_enabled = trace
        self.store = store or Store()
        self.lock = threading.RLock()
        self.transport: Elm327 | None = None
        self.vehicle: Vehicle | None = None
        self.trace: TraceLog | None = None
        self.book = RecipeBook.load_default()
        self.labels = self._load_labels()

    @property
    def sim(self) -> bool:
        return is_sim(self.adapter_url)

    def _load_labels(self) -> LabelDB:
        extra = [SIM_LABELS] if self.sim else []
        return LabelDB.load_default(extra_files=extra)

    def reload(self) -> None:
        self.book = RecipeBook.load_default()
        self.labels = self._load_labels()

    def connect(self, adapter: str | None = None) -> Vehicle:
        with self.lock:
            if adapter and adapter != self.adapter_url:
                self.disconnect()
                self.adapter_url = adapter
                self.labels = self._load_labels()
            if self.vehicle is not None:
                return self.vehicle
            path = None
            if self.trace_enabled:
                folder = sub_dir("logs")
                _rotate_logs(folder)
                path = os.path.join(folder, f"session_{time.strftime('%Y%m%d_%H%M%S')}.log")
            self.trace = TraceLog(path)
            self.trace("##", f"adapter {self.adapter_url}")
            kw = {}
            if self.sim:
                from .sim.car import make_sim_elm
                from urllib.parse import parse_qs, urlparse
                q = parse_qs(urlparse(self.adapter_url).query) if "?" in self.adapter_url else {}
                sfd = ",".join(q.get("sfd", [])).upper()
                state = None if q.get("fresh") else os.path.join(data_dir(), "sim_state.json")
                kw["sim_state"] = make_sim_elm(stn=q.get("stn", ["0"])[0] == "1", sfd_5f="5F" in sfd,
                                               sfd_17="17" in sfd, state_file=state)
            try:
                self.transport = open_adapter(self.adapter_url, trace=self.trace, st_min_ms=self.st_min_ms,
                                              **kw)
            except Exception:
                self.trace.close()
                raise
            self.vehicle = Vehicle(self.transport, self.store)
            return self.vehicle

    def disconnect(self) -> None:
        with self.lock:
            if self.transport is not None:
                try:
                    self.transport.close()
                except Exception:
                    pass
            self.transport = None
            self.vehicle = None
            if self.trace:
                self.trace.close()
                self.trace = None

    def require(self) -> Vehicle:
        return self.vehicle or self.connect()
