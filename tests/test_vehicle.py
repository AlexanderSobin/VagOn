"""Safety-critical write flow on the simulated car."""

import pytest

from vagon.errors import SafetyError, VagOnError
from vagon.sim.car import make_sim_elm
from vagon.sim.link import SimLink
from vagon.transport.elm327 import Elm327
from vagon.vag.coding import LongCoding
from vagon.vag.store import Store
from vagon.vag.vehicle import Vehicle

from .conftest import ecu


def test_scan_finds_modules(vehicle):
    found = [e for e in vehicle.scan() if e.found]
    addrs = {e.module.addr_hex for e in found}
    assert {"01", "09", "17", "19", "5F", "A5"} <= addrs
    assert vehicle.vin == "WAUZZZGY0MA000000"


def test_write_coding_full_sequence(sim, vehicle):
    s = vehicle.module("09")
    cur = s.read_coding()
    new = cur.copy()
    new.set_bit(3, 5, not cur.get_bit(3, 5))
    r = s.write_coding(new, reason="test", login=31347)
    assert r.verified and r.backup_path
    e = ecu(sim, 0x09)
    order = [did for did, _ in e.write_log]
    assert order == [0xF199, 0xF198, 0x0600], "workshop data must precede the coding write"
    assert e.dids[0x0600] == bytes(new)
    assert e.dids[0xF199] != bytes([0x20, 0x11, 0x05])       # today's BCD date written
    b = vehicle.store.load_backup(r.backup_path)
    assert b.dids["0x0600"] == bytes(cur).hex().upper()
    journal = vehicle.store.read_journal()
    assert journal[-1]["status"] == "ok" and journal[-1]["module"] == "09"


def test_auto_login_when_needed(sim, vehicle):
    s = vehicle.module("5F")
    r = s.write({0x0961: b"\x01"}, reason="t")
    assert r.verified and any("20103" in n for n in r.notes)


def test_no_change_no_write(sim, vehicle):
    s = vehicle.module("09")
    cur = bytes(s.read_coding())
    r = s.write_coding(cur, reason="noop")
    assert not r.written and r.backup_path is None
    assert ecu(sim, 0x09).write_log == []


def test_dry_run_writes_nothing(sim, vehicle):
    s = vehicle.module("09")
    lc = s.read_coding()
    lc.set_bit(0, 0, not lc.get_bit(0, 0))
    r = s.write_coding(lc, reason="dry", dry_run=True)
    assert r.written and ecu(sim, 0x09).write_log == []


def test_length_mismatch_refused(sim, vehicle):
    with pytest.raises(SafetyError):
        vehicle.module("09").write_coding(bytes(5), reason="bad")
    assert ecu(sim, 0x09).write_log == []


def test_protected_module_refused(vehicle):
    with pytest.raises(SafetyError):
        vehicle.module("15").write_coding(bytes(20), reason="x")
    with pytest.raises(SafetyError):
        vehicle.module("25").write({0x0600: b"\x00"}, reason="x")


def test_sfd_locked_reports_sfd(vagon_home):
    sim = make_sim_elm(sfd_5f=True)
    v = Vehicle(Elm327(SimLink(sim)).connect(), Store())
    with pytest.raises(VagOnError, match="SFD"):
        v.module("5F").write({0x0961: b"\x01"}, reason="t")
    assert ecu(sim, 0x5F).dids[0x0961] == b"\x00"
    assert v.store.read_journal()[-1]["status"] == "failed"


def test_wrong_login_not_retried(sim, vehicle):
    s = vehicle.module("09")
    lc = s.read_coding()
    lc.set_bit(0, 1, not lc.get_bit(0, 1))
    with pytest.raises(VagOnError, match="не принят"):
        s.write_coding(lc, reason="t", login=11111)
    with pytest.raises(SafetyError, match="повтор не делаю"):
        s.write_coding(lc, reason="t", login=11111)
    assert ecu(sim, 0x09)._bad_keys == 1


def test_restore_from_backup(sim, vehicle):
    s = vehicle.module("09")
    original = bytes(s.read_coding())
    lc = LongCoding(original)
    lc.set_bit(2, 2, not lc.get_bit(2, 2))
    r = s.write_coding(lc, reason="change", login=31347)
    b = vehicle.store.load_backup(r.backup_path)
    s.restore(b, login=31347)
    assert ecu(sim, 0x09).dids[0x0600] == original


def test_restore_other_module_refused(sim, vehicle):
    s = vehicle.module("09")
    lc = s.read_coding()
    lc.set_bit(2, 3, not lc.get_bit(2, 3))
    r = s.write_coding(lc, reason="change", login=31347)
    with pytest.raises(SafetyError):
        vehicle.module("17").restore(vehicle.store.load_backup(r.backup_path))


def test_dtc_read_clear(sim, vehicle):
    s = vehicle.module("09")
    assert s.read_dtcs() == [(0x9A0B00, 0x08)]
    s.clear_dtcs()
    assert s.read_dtcs() == []


def test_session_timeout_relogin(sim, vehicle):
    """S3 timeout between operations must not break the next write (fresh session each time)."""
    s = vehicle.module("09")
    lc = s.read_coding()
    lc.set_bit(4, 4, not lc.get_bit(4, 4))
    s.write_coding(lc, reason="a", login=31347)
    sim.clock.advance(30)
    lc.set_bit(4, 4, not lc.get_bit(4, 4))
    assert s.write_coding(lc, reason="b", login=31347).verified


# ---------------------------------------------------------------- review regressions
def test_lost_answer_but_write_landed_is_recovered(sim, vehicle):
    e = ecu(sim, 0x09)
    real = e._write

    def lossy(req):
        out = real(req)
        return [] if (req[1] << 8 | req[2]) == 0x0600 else out
    e._write = lossy
    s = vehicle.module("09")
    lc = s.read_coding()
    lc.set_bit(0, 0, not lc.get_bit(0, 0))
    r = s.write_coding(lc, reason="t", login=31347)
    assert r.verified and e.dids[0x0600] == bytes(lc)
    assert any("подтверждена чтением" in n for n in r.notes)
    assert vehicle.store.read_journal()[-1]["status"] == "ok"


def test_corrupted_write_echo_is_assessed(vagon_home):
    from .test_elm import FlakyLink
    from vagon.vag.vehicle import WriteFailed
    sim = make_sim_elm()
    link = FlakyLink(sim)
    v = Vehicle(Elm327(link).connect(), Store())
    s = v.module("09")
    lc = s.read_coding()
    lc.set_bit(0, 0, not lc.get_bit(0, 0))
    link.prefix = b"24"                    # last consecutive frame of the coding write
    with pytest.raises(WriteFailed) as ei:
        s.write_coding(lc, reason="t", login=31347)
    assert ei.value.status == "partial" and "vagon restore" in str(ei.value)
    assert ecu(sim, 0x09).dids[0x0600] != bytes(lc)       # module holds the corrupted value
    assert v.store.read_journal()[-1]["status"] == "partial"


def test_partial_multi_did_write_reported(sim, vehicle):
    from vagon.vag.vehicle import WriteFailed
    e = ecu(sim, 0x09)
    e.writable.discard(0x0903)
    with pytest.raises(WriteFailed) as ei:
        vehicle.module("09").write({0x0901: b"\x09", 0x0903: b"\x02"}, reason="t", login=31347)
    assert ei.value.status == "partial" and ei.value.applied == ["0x0901"]
    j = vehicle.store.read_journal()[-1]
    assert j["status"] == "partial" and list(j["changes"]) == ["0x0901"]


def test_stale_plan_refused(sim, vehicle):
    from vagon.app import SIM_LABELS
    from vagon.recipes import RecipeBook, apply_plan, plan_recipe
    from vagon.vag.labels import LabelDB
    r = RecipeBook.load_default().get("mmi.wireless_carplay")
    p = plan_recipe(vehicle, r, "on", LabelDB.load_default(extra_files=[SIM_LABELS]))
    ecu(sim, 0x5F).dids[0x0960] = b"\x04"            # changed in the MMI meanwhile
    with pytest.raises(VagOnError, match="изменился после проверки"):
        apply_plan(vehicle, p)
    assert ecu(sim, 0x5F).dids[0x0960] == b"\x04"


def test_restore_full_backup_skips_live_values(sim, vehicle):
    from vagon.vag.learn import sweep
    e = ecu(sim, 0x09)
    e.dids[0x0100] = b"\x7C"                           # read-only live value
    s = vehicle.module("09")
    vehicle.read_vin()
    data = sweep(s, [(0x0100, 0x0100), (0x0600, 0x0600), (0x0900, 0x0904)], vin=vehicle.vin)
    ident = s.ident(refresh=True)
    dids = {k: s.read_did(int(k, 16)).hex().upper() for k in data["dids"]}
    b = vehicle.store.save_backup(vin=vehicle.vin, module="09", ident=ident.to_dict(), coding=ident.coding,
                                  dids=dids, reason="full backup")
    lc = s.read_coding()
    lc.set_bit(0, 0, not lc.get_bit(0, 0))
    s.write_coding(lc, reason="tweak", login=31347)
    s.write({0x0903: b"\x05"}, reason="tweak2", login=31347)
    e.dids[0x0100] = b"\x7E"
    r = s.restore(b, login=31347)
    assert e.dids[0x0600] == bytes.fromhex(b.dids["0x0600"])
    assert e.dids[0x0903] == bytes.fromhex(b.dids["0x0903"])
    assert any("0x0100 пропущен" in n for n in r.notes) and r.verified


def test_restore_refuses_other_module_identity(sim, vehicle):
    s = vehicle.module("09")
    lc = s.read_coding()
    lc.set_bit(5, 5, not lc.get_bit(5, 5))
    r = s.write_coding(lc, reason="x", login=31347)
    b = vehicle.store.load_backup(r.backup_path)
    b.data["ident"]["part_number"] = "5WA937084ZZ"     # backup of a replaced module
    with pytest.raises(SafetyError, match="другого блока"):
        s.restore(b)
    s.restore(b, force=True, login=31347)


def test_low_voltage_refused(sim, vehicle, monkeypatch):
    sim.voltage = 11.2
    s = vehicle.module("09")
    lc = s.read_coding()
    lc.set_bit(6, 6, not lc.get_bit(6, 6))
    with pytest.raises(SafetyError, match="напряжение"):
        s.write_coding(lc, reason="x", login=31347)
    assert ecu(sim, 0x09).write_log == []
    monkeypatch.setattr(Vehicle, "min_write_voltage", 0)
    assert s.write_coding(lc, reason="x", login=31347).verified
