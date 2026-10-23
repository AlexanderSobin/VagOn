"""High-level, safety-first operations on the car and its modules.

Every write goes through ModuleSession.write():
  1. refuse protected modules (immobilizer, airbag, steering lock)
  2. re-read the current values; nothing to do → stop
  3. save a backup file (identification + current coding + every DID touched)
  4. extended (or requested) session, optional workshop login
  5. F199 programming date + F198 tester code (as ODIS/VCDS do; required by
     many modules before a coding write)
  6. write, then read back and compare; journal the result
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from ..errors import (NegativeResponse, ResponsePendingTimeout, SafetyError, TimeoutError_,
                      TransportError, VagOnError, VerifyError)
from ..hexutil import to_hex
from ..transport.base import Transport
from ..uds import SESSION_DEFAULT, SESSION_EXTENDED, UdsClient
from .coding import LongCoding
from .ident import (DID_CODING, DID_PROG_DATE, DID_TESTER_ID, DID_VIN, DID_WORKSHOP_CODE,
                    Identification, decode_text, read_identification)
from .modules import MODULES, Module, get_module
from .security import COMMON_LOGINS, LoginGuard, perform_login
from .store import Backup, Store

SESSION_DEVELOPMENT = 0x4F  # VAG development/engineering session (developer menus)

ProgressFn = Callable[[str], None]


def did_key(did: int) -> str:
    return f"0x{did:04X}"


def explain_write_error(e: NegativeResponse, *, login: int | None, module: Module) -> str:
    """Turn an NRC on a write into advice."""
    base = str(e)
    if e.nrc == 0x33:
        if login is None:
            return (base + "\nВероятные причины: нужен логин-код (Security Access) или блок защищён SFD. "
                    "Попробуйте рецепт/команду с --login (часто 20103/31347), или разблокируйте SFD "
                    "легальным способом (OBDeleven/ODIS онлайн) и повторите в течение окна разблокировки.")
        return (base + f"\nЛогин {login} принят, но запись запрещена — почти наверняка защита SFD "
                f"на блоке {module.addr_hex}. Разблокируйте SFD легальным способом (OBDeleven Pro/Ultimate, "
                "VCDS с токеном, ODIS с GeKo) и сразу повторите запись. VagOn не обходит SFD.")
    if e.nrc == 0x22:
        return (base + "\nПроверьте: зажигание ВКЛ, двигатель ВЫКЛ, скорость 0, напряжение >12 В. "
                "На некоторых MQB помогает открытый капот (снимает «диагностический firewall»).")
    if e.nrc == 0x24:
        return base + "\nБлок ждёт другой порядок шагов (сессия → логин → F199/F198 → запись)."
    if e.nrc == 0x31:
        return (base + "\nБлок не принимает это значение/идентификатор: функция не поддерживается "
                "этой версией ПО или рецепт не подходит к вашему блоку.")
    if e.nrc == 0x13:
        return base + "\nНеверная длина данных — рецепт не подходит к этому блоку."
    if e.nrc in (0x7F, 0x7E):
        return base + "\nНужна другая диагностическая сессия."
    return base


@dataclass
class WriteResult:
    module: str
    written: dict[str, tuple[str, str]] = field(default_factory=dict)  # did → (old, new)
    unchanged: list[str] = field(default_factory=list)
    backup_path: str | None = None
    verified: bool = False
    dry_run: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"module": self.module, "written": self.written, "unchanged": self.unchanged,
                "backup": self.backup_path, "verified": self.verified, "dry_run": self.dry_run,
                "notes": self.notes}


class ModuleSession:
    def __init__(self, vehicle: "Vehicle", module: Module):
        self.vehicle = vehicle
        self.module = module
        self.uds = UdsClient(vehicle.transport.channel(module.tx_id, module.rx_id))
        self._ident: Identification | None = None
        self._unlocked_with: int | None = None

    # ---------------------------------------------------------------- reading
    def ident(self, refresh: bool = False, quick: bool = False) -> Identification:
        if self._ident is None or refresh:
            self._ident = read_identification(self.uds, self.module.addr_hex, quick=quick)
        return self._ident

    def read_coding(self) -> LongCoding:
        return LongCoding(self.uds.read_did(DID_CODING))

    def read_did(self, did: int) -> bytes:
        return self.uds.read_did(did)

    def try_read_did(self, did: int) -> bytes | None:
        try:
            return self.uds.read_did(did)
        except NegativeResponse:
            return None

    def read_dtcs(self) -> list[tuple[int, int]]:
        _avail, dtcs = self.uds.read_dtc_by_status(0xFF)
        return dtcs

    def clear_dtcs(self) -> None:
        self.uds.clear_dtc()

    # ---------------------------------------------------------------- session
    def enter_session(self, session: int = SESSION_EXTENDED) -> None:
        self.uds.diagnostic_session(session)
        self._unlocked_with = None

    def default_session(self) -> None:
        try:
            self.uds.diagnostic_session(SESSION_DEFAULT)
        except (NegativeResponse, TimeoutError_):
            pass
        self._unlocked_with = None

    def login(self, code: int, force: bool = False) -> None:
        if self._unlocked_with == code:
            return
        perform_login(self.uds, self.module.address, code, self.vehicle.guard, force=force)
        self._unlocked_with = code

    def _workshop_prerequisites(self, notes: list[str]) -> None:
        """F199 (date, BCD YYMMDD) and F198 (tester code = current F1A5) like ODIS/VCDS."""
        date = bytes.fromhex(time.strftime("%y%m%d"))
        tester = None
        for did in (DID_WORKSHOP_CODE, DID_TESTER_ID):
            try:
                tester = self.uds.read_did(did)
                if tester:
                    break
            except NegativeResponse:
                continue
        for did, data, name in ((DID_PROG_DATE, date, "дата программирования F199"),
                                (DID_TESTER_ID, tester, "код мастерской F198")):
            if data is None:
                notes.append(f"{name}: нечего записать (F1A5 не читается) — пропуск")
                continue
            try:
                self.uds.write_did(did, data)
            except NegativeResponse as e:
                if e.nrc in (0x31, 0x11, 0x7F, 0x13):
                    notes.append(f"{name}: блок не поддерживает ({e.nrc:#04x}) — пропуск")
                    continue
                raise

    # ---------------------------------------------------------------- writing
    def write(self, changes: dict[int, bytes], *, reason: str, login: int | None = None,
              session: int = SESSION_EXTENDED, dry_run: bool = False, force_login: bool = False,
              auto_login: bool = True, progress: ProgressFn | None = None) -> WriteResult:
        say = progress or (lambda _m: None)
        if self.module.protected:
            raise SafetyError(f"блок {self.module.label()} защищён в VagOn — запись запрещена")
        if not changes:
            raise SafetyError("нет изменений")
        result = WriteResult(self.module.addr_hex, dry_run=dry_run)

        say("читаю текущие значения…")
        ident = self.ident(refresh=True)
        if not self.vehicle.vin and len(ident.vin) == 17:
            self.vehicle.vin = ident.vin
        current: dict[int, bytes] = {}
        for did, new in changes.items():
            cur = self.uds.read_did(did)
            if len(cur) != len(new):
                raise SafetyError(f"длина {did_key(did)} в блоке {len(cur)} байт, а записать хотят "
                                  f"{len(new)} — отмена (рецепт не подходит к этому блоку)")
            current[did] = cur
        todo = {did: new for did, new in changes.items() if current[did] != new}
        result.unchanged = [did_key(d) for d in changes if d not in todo]
        for did, new in todo.items():
            result.written[did_key(did)] = (current[did].hex().upper(), new.hex().upper())
        if not todo:
            result.verified = True
            result.notes.append("всё уже установлено — запись не нужна")
            return result
        if dry_run:
            return result

        say("сохраняю резервную копию…")
        backup = self.vehicle.store.save_backup(
            vin=self.vehicle.vin or ident.vin, module=self.module.addr_hex, ident=ident.to_dict(),
            coding=(current.get(DID_CODING) or b"").hex().upper() or ident.coding,
            dids={did_key(d): v.hex().upper() for d, v in current.items()}, reason=reason)
        result.backup_path = backup.path

        set_patient = getattr(self.vehicle.transport, "set_patient", None)
        if set_patient:
            set_patient(True)
        try:
            written: set[int] = set()
            attempt_login = login
            while True:
                try:
                    self._write_sequence(todo, written, session, attempt_login, force_login,
                                         result.notes, say)
                    break
                except NegativeResponse as e:
                    if (e.nrc == 0x33 and attempt_login is None and auto_login
                            and self.module.address in COMMON_LOGINS):
                        attempt_login = COMMON_LOGINS[self.module.address][0]
                        result.notes.append(f"блок потребовал логин — пробую типовой код {attempt_login}")
                        continue
                    self._journal(result, "failed", str(e), reason)
                    if e.sid == 0x27:
                        raise VagOnError(f"логин {attempt_login} не принят: {e}") from e
                    raise VagOnError(explain_write_error(e, login=attempt_login,
                                                         module=self.module)) from e
            say("проверяю…")
            bad = []
            for did, new in todo.items():
                got = self.uds.read_did(did)
                if got != new:
                    bad.append(f"{did_key(did)}: ожидалось {to_hex(new, '')}, в блоке {to_hex(got, '')}")
            if bad:
                self._journal(result, "verify_failed", "; ".join(bad), reason)
                raise VerifyError("блок принял запись, но хранит другое значение:\n" + "\n".join(bad) +
                                  "\n(часть битов может быть недоступна в этой версии ПО). "
                                  f"Резервная копия: {backup.path}")
            result.verified = True
            self._journal(result, "ok", "", reason)
            return result
        finally:
            if set_patient:
                try:
                    set_patient(False)
                except TransportError:
                    pass
            self._ident = None

    def _write_sequence(self, todo: dict[int, bytes], written: set[int], session: int,
                        login: int | None, force_login: bool, notes: list[str], say) -> None:
        """session → login → F199/F198 → writes. Raises NegativeResponse unchanged."""
        say("открываю сессию…")
        self.enter_session(session)
        if login is not None:
            say(f"логин {login}…")
            self.login(login, force=force_login)
        self._workshop_prerequisites(notes)
        for did, new in todo.items():
            if did in written:
                continue
            say(f"пишу {did_key(did)}…")
            try:
                self.uds.write_did(did, new)
            except ResponsePendingTimeout:
                notes.append(f"{did_key(did)}: ответ 'ожидайте' без итога — проверяю чтением")
            written.add(did)

    def _journal(self, result: WriteResult, status: str, error: str, reason: str) -> None:
        self.vehicle.store.journal(vin=self.vehicle.vin, module=self.module.addr_hex, status=status,
                                   reason=reason, changes=result.written, error=error,
                                   backup=result.backup_path)

    def write_coding(self, new: LongCoding | bytes, *, reason: str, login: int | None = None,
                     dry_run: bool = False, progress: ProgressFn | None = None) -> WriteResult:
        return self.write({DID_CODING: bytes(new)}, reason=reason, login=login, dry_run=dry_run,
                          progress=progress)

    def restore(self, backup: Backup, *, login: int | None = None, dry_run: bool = False,
                progress: ProgressFn | None = None) -> WriteResult:
        if backup.module.upper() != self.module.addr_hex:
            raise SafetyError(f"копия от блока {backup.module}, а выбран {self.module.addr_hex}")
        vin = self.vehicle.vin
        if vin and backup.data.get("vin") and backup.data["vin"] != vin:
            raise SafetyError(f"копия от другого автомобиля ({backup.data['vin']})")
        changes: dict[int, bytes] = {}
        for k, v in backup.dids.items():
            changes[int(k, 16)] = bytes.fromhex(v)
        if not changes and backup.coding:
            changes[DID_CODING] = bytes.fromhex(backup.coding)
        return self.write(changes, reason=f"restore {backup.created}", login=login, dry_run=dry_run,
                          progress=progress)


@dataclass
class ScanEntry:
    module: Module
    found: bool
    ident: Identification | None = None
    error: str = ""
    dtc_count: int | None = None


class Vehicle:
    def __init__(self, transport: Transport, store: Store | None = None):
        self.transport = transport
        self.store = store or Store()
        self.guard = LoginGuard()
        self._sessions: dict[int, ModuleSession] = {}
        self.vin: str | None = None
        self.found: list[ScanEntry] = []

    def module(self, addr) -> ModuleSession:
        m = get_module(addr)
        if m.address not in self._sessions:
            self._sessions[m.address] = ModuleSession(self, m)
        return self._sessions[m.address]

    def read_vin(self) -> str | None:
        for addr in ("19", "09", "01", "17"):
            try:
                vin = decode_text(self.module(addr).read_did(DID_VIN))
            except (NegativeResponse, TimeoutError_):
                continue
            if len(vin) == 17:
                self.vin = vin
                return vin
        return None

    def scan(self, *, progress: ProgressFn | None = None, with_dtc: bool = False,
             addresses: list | None = None) -> list[ScanEntry]:
        say = progress or (lambda _m: None)
        mods = [get_module(a) for a in addresses] if addresses else MODULES
        seen_tx: set[int] = set()
        out: list[ScanEntry] = []
        fast = getattr(self.transport, "fast_timeouts", None)
        if fast:
            fast(True)
        try:
            for m in mods:
                if m.tx_id in seen_tx:
                    continue
                say(f"{m.label()}…")
                sess = self.module(m.address)
                try:
                    ident = read_identification(sess.uds, m.addr_hex, quick=True, with_coding=False)
                except TimeoutError_:
                    out.append(ScanEntry(m, False))
                    continue
                except NegativeResponse as e:
                    ident = Identification(address=m.addr_hex, errors={"part_number": str(e)})
                except TransportError:
                    raise
                seen_tx.add(m.tx_id)
                entry = ScanEntry(m, True, ident)
                if with_dtc:
                    try:
                        entry.dtc_count = len(sess.read_dtcs())
                    except (NegativeResponse, TimeoutError_):
                        entry.dtc_count = None
                out.append(entry)
        finally:
            if fast:
                fast(False)
        self.found = [e for e in out if e.found]
        if self.vin is None:
            try:
                self.read_vin()
            except TransportError:
                pass
        return out
