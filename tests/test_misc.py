import os

import pytest

from vagon import paths
from vagon.app import App, _rotate_logs
from vagon.connect import open_adapter
from vagon.errors import SafetyError, TransportError
from vagon.vag.dtc import describe_status, format_dtc
from vagon.vag.modules import MODULES, get_module
from vagon.vag.security import LoginGuard, login_key
from vagon.vag.store import Store


def test_module_lookup():
    assert get_module("09").tx_id == 0x70E and get_module(9).rx_id == 0x778
    assert get_module("0x5f").rx_id == 0x7DD
    assert get_module("a5").addr_hex == "A5"
    for bad in ("ZZ", "", "999"):
        with pytest.raises(KeyError):
            get_module(bad)
    for m in MODULES:
        if m.tx_id < 0x7E0:
            assert m.rx_id == m.tx_id + 0x6A, m
    assert get_module("25").protected and get_module("15").protected


def test_login_key_math():
    assert login_key(b"\x00\x00\x00\x01", 20103) == (20104).to_bytes(4, "big")
    assert login_key(b"\xFF\xFF\xFF\xFF", 1) == b"\x00\x00\x00\x00"       # wraps mod 2^32
    with pytest.raises(SafetyError):
        login_key(b"\x01\x02", 20103)
    g = LoginGuard()
    g.failed.add((0x09, 11111))
    with pytest.raises(SafetyError):
        g.check(0x09, 11111)
    g.check(0x09, 11111, force=True)
    g.check(0x17, 11111)


def test_dtc_format():
    assert format_dtc(0x9A0B00) == "B1A0B 00"
    assert format_dtc(0x000000) == "P0000 00"
    assert format_dtc(0xC12345) == "U0123 45"
    assert "подтверждённая" in describe_status(0x08)
    assert describe_status(0) == "нет флагов"


def test_store_backups_and_filter(tmp_path):
    st = Store(str(tmp_path))
    a = st.save_backup(vin="V1", module="09", ident={}, coding="00", dids={"0x0600": "00"}, reason="r/1")
    b = st.save_backup(vin="V1", module="09", ident={}, coding="01", dids={"0x0600": "01"}, reason="r/1")
    st.save_backup(vin="V2", module="17", ident={}, coding=None, dids={}, reason="x")
    assert a.path != b.path and os.path.exists(a.path) and os.path.exists(b.path)
    assert len(st.list_backups(vin="V1")) == 2
    assert [x.module for x in st.list_backups(module="17")] == ["17"]
    (tmp_path / "backups" / "junk.json").write_text("{not json")
    assert len(st.list_backups()) == 3
    with pytest.raises(ValueError):
        Store.load_backup(_write(tmp_path / "other.json", '{"a": 1}'))
    st.journal(module="09", status="ok")
    assert st.read_journal()[-1]["status"] == "ok"


def _write(path, text):
    path.write_text(text)
    return str(path)


def test_open_adapter_urls():
    elm = open_adapter("sim://?stn=1")
    assert elm.is_stn
    with pytest.raises(TransportError):
        open_adapter("bogus://x")
    with pytest.raises(TransportError):
        open_adapter("tcp://127.0.0.1:1")      # nothing listens there


def test_app_sim_state_persists():
    app = App("sim", trace=False)
    v = app.connect()
    s = v.module("09")
    s.write({0x0901: b"\x09"}, reason="t", login=31347)
    app.disconnect()
    app2 = App("sim", trace=False)
    assert app2.connect().module("09").read_did(0x0901) == b"\x09"
    app2.disconnect()
    fresh = App("sim://?fresh=1", trace=False)
    assert fresh.connect().module("09").read_did(0x0901) == b"\x01"
    fresh.disconnect()


def test_trace_log_written_and_rotated(tmp_path):
    app = App("sim://?fresh=1", trace=True)
    app.connect()
    path = app.trace.path
    app.disconnect()
    text = open(path, encoding="utf-8").read()
    assert ">> ATZ" in text and "<<" in text
    folder = tmp_path / "logs"
    folder.mkdir()
    for i in range(40):
        (folder / f"session_{i:04d}.log").write_text("x")
    _rotate_logs(str(folder))
    assert len(list(folder.iterdir())) == 30


def test_data_dir_ios(monkeypatch, tmp_path):
    monkeypatch.delenv("VAGON_HOME")
    monkeypatch.setattr(paths, "is_ios", lambda: True)
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)
    assert paths.data_dir() == os.path.join(str(tmp_path), "Documents", "VagOn")
