"""A simulated Audi A3 8Y (MQB-Evo) for tests and the offline demo.

Identification strings are fake (marked SIM); coding lengths and the general
behaviour mimic the real modules. Some modules are SFD-locked so the demo
shows how VagOn reports SFD protection.
"""

from __future__ import annotations

import random

from ..vag.modules import get_module
from .ecu import DID_CODING, SimEcu
from .elm import SimElm327, VirtualClock

VIN = "WAUZZZGY0MA000000"


def _txt(s: str, n: int) -> bytes:
    return s.ljust(n)[:n].encode("ascii")


def _ecu(addr: str, *, part: str, sw: str, hw: str, system: str, dataset: str,
         coding_len: int, login: int | None, seed: int, sfd: bool = False,
         extra_dids: dict[int, bytes] | None = None, writable_extra: set[int] | None = None,
         dtcs=None, coding: bytes | None = None, did_sessions: dict[int, int] | None = None) -> SimEcu:
    m = get_module(addr)
    rng = random.Random(seed)
    if coding is None:
        coding = bytes(rng.getrandbits(8) for _ in range(coding_len))
    dids = {
        0xF187: _txt(part, 11),
        0xF189: _txt(sw, 4),
        0xF191: _txt(hw, 11),
        0xF1A3: _txt("H01", 3),
        0xF197: _txt(system, 13),
        0xF19E: (dataset + "\x00").encode("ascii"),
        0xF1A2: _txt("001001", 6),
        0xF190: VIN.encode("ascii"),
        0xF18C: _txt(f"SIM{seed:011d}", 14),
        0xF1A5: bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
        0xF198: bytes([0x00, 0x00, 0x00, 0x00, 0x00, 0x00]),
        0xF199: bytes([0x20, 0x11, 0x05]),
        DID_CODING: coding,
    }
    dids.update(extra_dids or {})
    writable = {DID_CODING, 0xF198, 0xF199} | set(writable_extra or ())
    return SimEcu(address=m.address, name=m.name_en, tx_id=m.tx_id, rx_id=m.rx_id, dids=dids,
                  writable=writable, login=login, sfd_locked=sfd, dtcs=list(dtcs or []),
                  did_sessions=dict(did_sessions or {}))


def build_a3_8y(*, sfd_5f: bool = False, sfd_17: bool = False) -> list[SimEcu]:
    """Modules of a typical A3 8Y Sportback with MIB3 and Virtual Cockpit."""
    # Adaptation-like DIDs. Their channel names live in sim/labels_sim.json, which is
    # only loaded in simulator mode (dataset names contain "SIM" so they can never
    # match a real module).
    bcm_adapt = {
        0x0901: bytes([0x01]),          # coming home duration (x10 s) — for learn demos
        0x0902: bytes([0x01]),          # Intelligente Heckleuchte-Verbau (variant 1)
        0x0903: bytes([0x01]),          # leaving home duration (x10 s)
        0x0904: bytes([0x0F]),          # LTM_* light-off restrictions (4 flags, all active)
    }
    mib_adapt = {
        0x0960: bytes([0x00]),          # Apple_DIO / Apple_DIO_Wireless / Google_GAL_Wireless
        0x0961: bytes([0x00]),          # 5_ghz_wlan
        0x0963: bytes([0x00]),          # Wifi client 5 GHz / hotspot 5 GHz / WiFi_Client_HMI
        0x0970: bytes([0x00]),          # Developer mode
        0x0971: bytes([0x00]),          # car menu display items
        0x0972: bytes([0x00]),          # BAP: traffic_sign_recognition_0x21
    }
    gw_adapt = {0x0905: bytes([76])}     # Start_stop_voltage_limit, 0.1 V/bit → 7.6 V
    kombi_adapt = {0x0980: bytes([0x00])}  # Laptimer
    return [
        _ecu("01", part="05E906012AB", sw="0002", hw="05E907309B", system="R4 1.5l TSI",
             dataset="EV_ECMSIM15TSI", coding_len=25, login=None, seed=1,
             dtcs=[]),
        _ecu("02", part="0GC300041AB", sw="2101", hw="0GC927711", system="DQ381-7F MQB",
             dataset="EV_TCMSIMDQ381", coding_len=12, login=None, seed=2),
        _ecu("03", part="5Q0907379BT", sw="0204", hw="5Q0614517", system="ESC",
             dataset="EV_Brake1SIM", coding_len=30, login=40168, seed=3),
        _ecu("09", part="5WA937084AB", sw="0213", hw="5WA937084", system="BCM MQBEvo",
             dataset="EV_BCMSIM_AU38X", coding_len=30, login=31347, seed=9,
             extra_dids=bcm_adapt, writable_extra=set(bcm_adapt),
             dtcs=[(0x9A0B00, 0x08)]),
        _ecu("13", part="2Q0907572AB", sw="0130", hw="2Q0907572", system="ACC MRR",
             dataset="EV_ACCSIM", coding_len=18, login=20103, seed=13),
        _ecu("15", part="5WA959655B", sw="0451", hw="5WA959655", system="Airbag",
             dataset="EV_AirbaSIM", coding_len=20, login=None, seed=15),
        _ecu("17", part="8Y0920790A", sw="0352", hw="8Y0920790", system="KOMBI",
             dataset="EV_KOMBISIM_AU38X", coding_len=20, login=20103, seed=17, sfd=sfd_17,
             extra_dids=kombi_adapt, writable_extra=set(kombi_adapt)),
        _ecu("19", part="5WA907530A", sw="1022", hw="5WA907530", system="GW MQBevo",
             dataset="EV_GWSIM_AU38X", coding_len=24, login=20103, seed=19,
             extra_dids=gw_adapt, writable_extra=set(gw_adapt)),
        _ecu("44", part="5WA909144B", sw="3043", hw="5WA909144", system="EPS_ZFLS",
             dataset="EV_SteerAssisSIM", coding_len=4, login=19249, seed=44),
        _ecu("5F", part="8Y0035878E", sw="3620", hw="8Y0035878", system="MU-H-ND-EU",
             dataset="EV_MUH4SIM_AU38X", coding_len=29, login=20103, seed=0x5F, sfd=sfd_5f,
             extra_dids=mib_adapt, writable_extra=set(mib_adapt), did_sessions={0x0970: 0x4F}),
        _ecu("A5", part="2Q0980654B", sw="0560", hw="2Q0980654", system="MFK",
             dataset="EV_CamFrontSIM", coding_len=20, login=20103, seed=0xA5),
        _ecu("42", part="5WA959393A", sw="0050", hw="5WA959393", system="TSG FT",
             dataset="EV_DoorDrvSIM", coding_len=8, login=None, seed=42),
        _ecu("52", part="5WA959392A", sw="0050", hw="5WA959392", system="TSG BT",
             dataset="EV_DoorPasSIM", coding_len=8, login=None, seed=52),
        _ecu("65", part="5Q0907273D", sw="0013", hw="5Q0907273", system="RDK",
             dataset="EV_TPMSSIM", coding_len=6, login=None, seed=65),
        _ecu("76", part="5Q0919294N", sw="0110", hw="5Q0919294", system="PDC",
             dataset="EV_PDCSIM", coding_len=10, login=None, seed=76),
    ]


def make_sim_elm(*, stn: bool = False, sfd_5f: bool = False, sfd_17: bool = False,
                 state_file: str | None = None, **kw) -> SimElm327:
    """Build the simulated car. With `state_file`, module data survives restarts
    (so CLI commands run one after another see each other's changes)."""
    clock = VirtualClock()
    ecus = build_a3_8y(sfd_5f=sfd_5f, sfd_17=sfd_17)
    if state_file:
        load_state(ecus, state_file)
    elm = SimElm327(ecus, stn=stn, clock=clock, **kw)
    if state_file:
        elm.on_change = lambda: save_state(ecus, state_file)
    return elm


def save_state(ecus: list[SimEcu], path: str) -> None:
    import json
    import os
    data = {f"{e.address:02X}": {"dids": {f"{d:04X}": v.hex() for d, v in e.dids.items()},
                                  "dtcs": [[c, s] for c, s in e.dtcs]} for e in ecus}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def load_state(ecus: list[SimEcu], path: str) -> None:
    import json
    import os
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return
    for e in ecus:
        st = data.get(f"{e.address:02X}")
        if not st:
            continue
        for d, v in st.get("dids", {}).items():
            did = int(d, 16)
            if did in e.dids and len(bytes.fromhex(v)) == len(e.dids[did]):
                e.dids[did] = bytes.fromhex(v)
        e.dtcs = [(int(c), int(s)) for c, s in st.get("dtcs", [])]
