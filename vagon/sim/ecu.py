"""Simulated VAG UDS control module (for tests and the offline demo).

Behaviour modelled on real MQB/MQB-Evo modules:
* writes need the extended session (0x10 0x03), else NRC 0x7F
* security access level 3: key = seed + login code (32-bit), 3 wrong keys →
  0x36, then 0x37 for a delay
* coding write (DID 0x0600) needs F199 (programming date) and F198 (tester
  ID) written first in the same session, else NRC 0x24 (as observed by
  openpilot on MQB EPS)
* coding length is fixed: other lengths → NRC 0x13
* SFD-protected modules refuse all writes with NRC 0x33 until unlocked
* session falls back to default after S3 (5 s) without traffic
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Callable

from .. import isotp

DID_CODING = 0x0600


@dataclass
class SimEcu:
    address: int
    name: str
    tx_id: int  # tester → module (request id)
    rx_id: int  # module → tester (response id)
    dids: dict[int, bytes] = field(default_factory=dict)
    writable: set[int] = field(default_factory=set)
    login: int | None = None            # login code required for writes (None = no login)
    login_needed_for_coding: bool = True
    sfd_locked: bool = False
    require_prog_date: bool = True
    pending_on_write: int = 0           # number of 0x78 before the final write answer
    dtcs: list[tuple[int, int]] = field(default_factory=list)
    did_sessions: dict[int, int] = field(default_factory=dict)  # DID writable only in this session
    s3_timeout: float = 5.0
    clock: Callable[[], float] = field(default=lambda: 0.0)
    write_log: list[tuple[int, bytes]] = field(default_factory=list)

    session: int = 1
    unlocked: bool = False
    _seed: bytes | None = None
    _bad_keys: int = 0
    _lock_until: float = 0.0
    _prog_date_written: bool = False
    _tester_written: bool = False
    _last_rx: float = 0.0
    _rng: random.Random = field(default_factory=lambda: random.Random(1234))

    # ------------------------------------------------------------------- state
    def _touch(self) -> None:
        now = self.clock()
        if self.session != 1 and now - self._last_rx > self.s3_timeout:
            self._to_default()
        self._last_rx = now

    def _to_default(self) -> None:
        self.session = 1
        self.unlocked = False
        self._seed = None
        self._prog_date_written = False
        self._tester_written = False

    @staticmethod
    def _neg(sid: int, nrc: int) -> bytes:
        return bytes([0x7F, sid, nrc])

    # ----------------------------------------------------------------- service
    def handle(self, req: bytes) -> list[bytes]:
        """Process one complete UDS request, return list of responses (pending first)."""
        self._touch()
        if not req:
            return []
        sid = req[0]
        h = {
            0x10: self._session, 0x22: self._read, 0x2E: self._write, 0x27: self._security,
            0x3E: self._tester_present, 0x19: self._read_dtc, 0x14: self._clear_dtc,
            0x11: self._reset,
        }.get(sid)
        if h is None:
            return [self._neg(sid, 0x11)]
        return h(req)

    def _session(self, req):
        if len(req) != 2:
            return [self._neg(0x10, 0x13)]
        sub = req[1] & 0x7F
        if sub not in (1, 3, 0x4F):
            return [self._neg(0x10, 0x12)]
        if sub == 1:
            self._to_default()
        else:
            if self.session != sub:
                self._to_default()
            self.session = sub
        return [bytes([0x50, sub, 0x00, 0x32, 0x01, 0xF4])]

    def _tester_present(self, req):
        if len(req) != 2 or (req[1] & 0x7F) != 0:
            return [self._neg(0x3E, 0x12)]
        if req[1] & 0x80:
            return []
        return [b"\x7E\x00"]

    def _read(self, req):
        if len(req) != 3:
            return [self._neg(0x22, 0x13)]
        did = (req[1] << 8) | req[2]
        if did not in self.dids:
            return [self._neg(0x22, 0x31)]
        return [bytes([0x62, req[1], req[2]]) + self.dids[did]]

    def _security(self, req):
        if len(req) < 2:
            return [self._neg(0x27, 0x13)]
        sub = req[1]
        if self.session == 1:
            return [self._neg(0x27, 0x7F)]
        if sub != 0x03 and sub != 0x04:
            return [self._neg(0x27, 0x12)]
        if self.clock() < self._lock_until:
            return [self._neg(0x27, 0x37)]
        if sub == 0x03:
            if self.unlocked:
                return [b"\x67\x03\x00\x00\x00\x00"]
            self._seed = self._rng.getrandbits(32).to_bytes(4, "big")
            return [b"\x67\x03" + self._seed]
        if self._seed is None:
            return [self._neg(0x27, 0x24)]
        if len(req) != 6:
            return [self._neg(0x27, 0x13)]
        expected = ((int.from_bytes(self._seed, "big") + (self.login or 0)) & 0xFFFFFFFF)
        self._seed = None
        if int.from_bytes(req[2:6], "big") != expected:
            self._bad_keys += 1
            if self._bad_keys >= 3:
                self._lock_until = self.clock() + 600
                self._bad_keys = 0
                return [self._neg(0x27, 0x36)]
            return [self._neg(0x27, 0x35)]
        self._bad_keys = 0
        self.unlocked = True
        return [b"\x67\x04"]

    def _write(self, req):
        if len(req) < 4:
            return [self._neg(0x2E, 0x13)]
        did = (req[1] << 8) | req[2]
        data = bytes(req[3:])
        if self.session == 1:
            return [self._neg(0x2E, 0x7F)]
        if did not in self.writable:
            return [self._neg(0x2E, 0x31)]
        if did in self.did_sessions and self.session != self.did_sessions[did]:
            return [self._neg(0x2E, 0x31)]
        if self.sfd_locked:
            return [self._neg(0x2E, 0x33)]
        needs_login = self.login is not None and (did != DID_CODING or self.login_needed_for_coding)
        if did in (0xF198, 0xF199):
            needs_login = False
        if needs_login and not self.unlocked:
            return [self._neg(0x2E, 0x33)]
        if did in self.dids and len(self.dids[did]) != len(data):
            return [self._neg(0x2E, 0x13)]
        if did == DID_CODING and self.require_prog_date and not (
                self._prog_date_written and self._tester_written):
            return [self._neg(0x2E, 0x24)]
        if did == 0xF199:
            self._prog_date_written = True
        if did == 0xF198:
            self._tester_written = True
        self.dids[did] = data
        self.write_log.append((did, data))
        out = [self._neg(0x2E, 0x78)] * self.pending_on_write
        return out + [bytes([0x6E, req[1], req[2]])]

    def _read_dtc(self, req):
        if len(req) != 3 or req[1] != 0x02:
            return [self._neg(0x19, 0x12)]
        mask = req[2]
        body = bytearray([0x59, 0x02, 0xFF])
        for code, status in self.dtcs:
            if status & mask:
                body += bytes([(code >> 16) & 0xFF, (code >> 8) & 0xFF, code & 0xFF, status])
        return [bytes(body)]

    def _clear_dtc(self, req):
        if len(req) != 4:
            return [self._neg(0x14, 0x13)]
        self.dtcs.clear()
        return [b"\x54"]

    def _reset(self, req):
        if len(req) != 2:
            return [self._neg(0x11, 0x13)]
        self._to_default()
        return [bytes([0x51, req[1]])]


class IsoTpServer:
    """ISO-TP endpoint of a SimEcu on the virtual bus (classic CAN, 8-byte frames)."""

    N_CR = 1.0  # max wait for the next consecutive frame (s)
    N_BS = 1.0  # max wait for a flow control frame (s)

    def __init__(self, ecu: SimEcu, clock: Callable[[], float], bs: int = 0, st_min: int = 0):
        self.ecu = ecu
        self.clock = clock
        self.bs = bs
        self.st_min = st_min
        self._rx = isotp.Reassembler()
        self._rx_deadline = 0.0
        self._rx_count_in_block = 0
        self._tx_pending: list[bytes] = []   # consecutive frames waiting for FC
        self._tx_deadline = 0.0
        self._queued_after_tx: list[bytes] = []
        self.dropped_fc = 0

    def on_frame(self, can_id: int, data: bytes) -> list[bytes]:
        """A frame arrived; return frames the module sends in reply (on its rx_id)."""
        now = self.clock()
        if can_id != self.ecu.tx_id:
            return []
        t = data[0] >> 4
        if t == isotp.FC:
            if not self._tx_pending:
                return []
            if now > self._tx_deadline:
                self._tx_pending.clear()
                return []
            fc = isotp.parse_flow_control(data)
            if fc.status != isotp.FC_CTS:
                return []
            frames = self._tx_pending
            if fc.block_size:
                out, self._tx_pending = frames[:fc.block_size], frames[fc.block_size:]
                self._tx_deadline = now + self.N_BS
            else:
                out, self._tx_pending = frames, []
            if not self._tx_pending and self._queued_after_tx:
                more = self._queued_after_tx
                self._queued_after_tx = []
                for m in more:
                    out += self._send(m)
            return out
        if t == isotp.CF and self._rx.in_progress and now > self._rx_deadline:
            self._rx.reset()  # tester too slow: N_Cr expired
            return []
        try:
            msg = self._rx.feed(data)
        except Exception:
            self._rx.reset()
            return []
        if t == isotp.FF:
            self._rx_deadline = now + self.N_CR
            self._rx_count_in_block = 0
            return [isotp.flow_control(isotp.FC_CTS, self.bs, self.st_min)]
        if t == isotp.CF:
            self._rx_deadline = now + self.N_CR
            self._rx_count_in_block += 1
            if msg is None and self.bs and self._rx_count_in_block >= self.bs:
                self._rx_count_in_block = 0
                return [isotp.flow_control(isotp.FC_CTS, self.bs, self.st_min)]
        if msg is None:
            return []
        out: list[bytes] = []
        for resp in self.ecu.handle(msg):
            if self._tx_pending:
                self._queued_after_tx.append(resp)
            else:
                out += self._send(resp)
        return out

    def _send(self, payload: bytes) -> list[bytes]:
        frames = isotp.encode(payload, pad=0xAA)
        if len(frames) == 1:
            return frames
        self._tx_pending = frames[1:]
        self._tx_deadline = self.clock() + self.N_BS
        return frames[:1]
