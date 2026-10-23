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
