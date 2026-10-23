"""ELM327 driver against the byte-level emulator, including the real-world traps."""

import pytest

from vagon.errors import AdapterError, IsoTpError, TimeoutError_
from vagon.sim.car import make_sim_elm
from vagon.sim.link import SimLink
from vagon.transport.elm327 import Elm327
from vagon.uds import UdsClient
from vagon.vag.modules import get_module

from .conftest import ecu


def client(elm, addr="09"):
    m = get_module(addr)
    return UdsClient(elm.channel(m.tx_id, m.rx_id))


def test_connect_info(elm):
    info = elm.info()
    assert "ELM327" in info["version"] and info["voltage"] == 12.6 and not info["stn"]


def test_setup_commands(sim, elm):
    seen = [ln.upper() for ln in sim.lines_seen]
    for cmd in ("ATZ", "ATS1", "ATH1", "ATSP6", "ATCAF1", "ATCFC1"):
        assert cmd in seen
    assert "" not in sim.lines_seen, "a bare CR repeats the last command on a real ELM327"


def test_single_and_multiframe_read(sim, elm):
    u = client(elm)
    assert u.read_did(0xF187) == b"5WA937084AB"
    coding = u.read_did(0x0600)
    assert coding == ecu(sim, 0x09).dids[0x0600] and len(coding) == 30


def test_flow_control_goes_to_request_id(sim, elm):
    client(elm, "09").read_did(0x0600)
    fcs = [(cid, f) for cid, f in sim.frames_sent if f[0] >> 4 == 3]
    assert fcs and all(cid == 0x70E for cid, _ in fcs)


def test_trap_mode0_flow_control_breaks_vag_reads(sim, elm):
    """Proves the emulator models the trap: default FC mode sends FC to rx-8 (0x770)."""
    client(elm).read_did(0xF187)          # sets the target
    elm._cmd("ATFCSM0")
    with pytest.raises(IsoTpError):
        client(elm).read_did(0x0600)


def test_manual_multiframe_write(sim, elm):
    u = client(elm)
    u.diagnostic_session(3)
    seed = u.request_seed(3)
    u.send_key(3, ((int.from_bytes(seed, "big") + 31347) & 0xFFFFFFFF).to_bytes(4, "big"))
    u.write_did(0xF199, b"\x26\x10\x23")
    u.write_did(0xF198, bytes(6))
    new = bytes(range(30))
    u.write_did(0x0600, new)
    assert ecu(sim, 0x09).dids[0x0600] == new
    assert "ATR0" in [ln.upper() for ln in sim.lines_seen], "consecutive frames must not wait for replies"


def test_trap_slow_listen_after_fc_breaks_write(sim, elm, monkeypatch):
    """With ATST FF after the First Frame the adapter waits ~1 s → module N_Cr expires."""
    monkeypatch.setattr(Elm327, "FC_LISTEN_ST", 0xFF)
    u = client(elm)
    u.diagnostic_session(3)
    with pytest.raises(TimeoutError_):
        u.write_did(0xF198, bytes(6))       # 9-byte request → multi-frame


def test_stpx_path(vagon_home):
    sim = make_sim_elm(stn=True)
    elm = Elm327(SimLink(sim)).connect()
    assert elm.is_stn and elm.use_stpx
    u = client(elm, "17")
    u.diagnostic_session(3)
    seed = u.request_seed(3)
    u.send_key(3, ((int.from_bytes(seed, "big") + 20103) & 0xFFFFFFFF).to_bytes(4, "big"))
    u.write_did(0xF199, b"\x26\x10\x23")
    u.write_did(0xF198, bytes(6))
    u.write_did(0x0600, bytes(20))
    assert ecu(sim, 0x17).dids[0x0600] == bytes(20)
    assert any(ln.upper().startswith("STPX") for ln in sim.lines_seen)


def test_clone_without_fcsm_uses_manual_flow_control(vagon_home):
    sim = make_sim_elm(bad_fcsm=True)
    elm = Elm327(SimLink(sim)).connect()
    u = client(elm)
    assert u.read_did(0xF189) == b"0213"             # single frame
    assert u.read_did(0x0600) == ecu(sim, 0x09).dids[0x0600]   # multi-frame via our own FC
    assert not sim.cfc, "adapter auto-FC must be off (it would address another module)"
    fcs = [(cid, f) for cid, f in sim.frames_sent if f[0] >> 4 == 3]
    assert fcs and all(cid == 0x70E for cid, _ in fcs)
    # and a full coding write works on such a clone too
    u.diagnostic_session(3)
    seed = u.request_seed(3)
    u.send_key(3, ((int.from_bytes(seed, "big") + 31347) & 0xFFFFFFFF).to_bytes(4, "big"))
    u.write_did(0xF199, b"\x26\x10\x23")
    u.write_did(0xF198, bytes(6))
    u.write_did(0x0600, bytes(30))
    assert ecu(sim, 0x09).dids[0x0600] == bytes(30)


def test_echo_glued_clone(vagon_home):
    sim = make_sim_elm(echo_glued=True)
    elm = Elm327(SimLink(sim)).connect()
    assert client(elm).read_did(0x0600) == ecu(sim, 0x09).dids[0x0600]


def test_absent_module_times_out(elm):
    with pytest.raises(TimeoutError_):
        client(elm, "82").read_did(0xF187)


def test_can_error_reported(sim, elm):
    sim.protocol = 3
    elm._tx_id = None
    with pytest.raises(AdapterError, match="CAN ERROR"):
        client(elm).read_did(0xF187)


def test_parse_dlc_variant(elm):
    assert elm._parse_frames(["778 8 03 62 F1 87 55 55 55 55"], 0x778) == [bytes.fromhex("0362F18755555555")]
    assert elm._parse_frames(["779 03 62 F1 87"], 0x778) == []   # foreign id ignored
    with pytest.raises(AdapterError):
        elm._parse_frames(["BUFFER FULL"], 0x778)


def test_tcp_link_end_to_end(vagon_home):
    import socket
    import threading
    from vagon.sim.link import serve_tcp
    from vagon.transport.links import TcpLink
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    ready, stop = threading.Event(), threading.Event()
    t = threading.Thread(target=serve_tcp, args=(make_sim_elm,), kwargs=dict(port=port, ready=ready, stop=stop),
                         daemon=True)
    t.start()
    assert ready.wait(5)
    try:
        elm = Elm327(TcpLink("127.0.0.1", port)).connect()
        assert client(elm).read_did(0x0600)
        elm.close()
    finally:
        stop.set()
        t.join(3)
