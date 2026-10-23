"""Byte-level ELM327 emulator wired to simulated ECUs on a virtual CAN bus.

It reproduces the adapter behaviours the VagOn driver depends on, including the
traps: flow-control mode 0 sends FC to (response id - 8) which VAG modules
never see; with CAF1 only single frames (≤7 bytes) can be sent; after the last
received frame the adapter keeps listening for ATST before printing '>'
(modelled on a virtual clock so ISO-TP timeouts in the ECU are exercised).
"""

from __future__ import annotations

import re

from .. import isotp
from .ecu import IsoTpServer, SimEcu


class VirtualClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class SimElm327:
    BANNER = "ELM327 v1.5"

    def __init__(self, ecus: list[SimEcu], *, stn: bool = False, clock: VirtualClock | None = None,
                 voltage: float = 12.6, bad_fcsm: bool = False, echo_glued: bool = False):
        self.clock = clock or VirtualClock()
        self.ecus = ecus
        for e in ecus:
            e.clock = self.clock
        self.servers = [IsoTpServer(e, self.clock) for e in ecus]
        self.stn = stn
        self.voltage = voltage
        self.bad_fcsm = bad_fcsm      # emulate a clone without ATFCSM support
        self.echo_glued = echo_glued  # emulate clones that omit CR after the echo
        self.out = bytearray()
        self._inbuf = bytearray()
        self.lines_seen: list[str] = []
        self.frames_sent: list[tuple[int, bytes]] = []
        self.on_change = None  # called after a line that changed module data
        self.reset()

    # -------------------------------------------------------------- settings
    def reset(self) -> None:
        self.echo = True
        self.linefeed = False
        self.spaces = True
        self.headers = False
        self.dlc = False
        self.caf = True
        self.cfc = True
        self.protocol = 0
        self.adaptive = 1
        self.st = 0x32
        self.responses = True
        self.header = 0x7DF
        self.cra: int | None = None
        self.fc_header: int | None = None
        self.fc_data: bytes | None = None
        self.fc_mode = 0

    # ------------------------------------------------------------- byte API
    def write(self, data: bytes) -> None:
        for b in data:
            if b == 0x0D:
                line = self._inbuf.decode("ascii", "replace")
                self._inbuf.clear()
                self._process(line)
            elif b == 0x0A or b == 0x00:
                continue
            else:
                self._inbuf.append(b)

    def read(self) -> bytes:
        data = bytes(self.out)
        self.out.clear()
        return data

    # ------------------------------------------------------------- processing
    def _emit(self, text: str) -> None:
        self.out += text.encode("ascii")

    def _nl(self) -> str:
        return "\r\n" if self.linefeed else "\r"

    def _finish(self, lines: list[str]) -> None:
        nl = self._nl()
        self._emit("".join(ln + nl for ln in lines) + nl + ">")

    def _process(self, raw: str) -> None:
        before = sum(len(e.write_log) for e in self.ecus), sum(len(e.dtcs) for e in self.ecus)
        try:
            self._process_line(raw)
        finally:
            after = sum(len(e.write_log) for e in self.ecus), sum(len(e.dtcs) for e in self.ecus)
            if after != before and self.on_change:
                self.on_change()

    def _process_line(self, raw: str) -> None:
        self.clock.advance(0.003)
        self.lines_seen.append(raw)
        if self.echo:
            self._emit(raw if self.echo_glued else raw + self._nl())
        line = raw.replace(" ", "").upper()
        if not line:
            self._finish(["?"])  # VagOn must never send an empty line
            return
        if line.startswith("AT"):
            self._finish(self._at(line[2:]))
            return
        if line.startswith("ST"):
            self._finish(self._st(raw.strip()))
            return
        if not re.fullmatch(r"[0-9A-F]+", line) or len(line) % 2:
            self._finish(["?"])
            return
        data = bytes.fromhex(line)
        self._finish(self._send_request(data))

    def _at(self, cmd: str) -> list[str]:
        ok = ["OK"]
        if cmd == "Z":
            self.reset()
            return ["", self.BANNER]
        if cmd == "WS":
            self.reset()
            return [self.BANNER]
        if cmd == "I":
            return [self.BANNER]
        if cmd == "@1":
            return ["OBDII to RS232 Interpreter (SIM)"]
        if cmd == "RV":
            return [f"{self.voltage:.1f}V"]
        flags = {"E0": ("echo", False), "E1": ("echo", True), "L0": ("linefeed", False),
                 "L1": ("linefeed", True), "S0": ("spaces", False), "S1": ("spaces", True),
                 "H0": ("headers", False), "H1": ("headers", True), "D0": ("dlc", False),
                 "D1": ("dlc", True), "CAF0": ("caf", False), "CAF1": ("caf", True),
                 "CFC0": ("cfc", False), "CFC1": ("cfc", True), "R0": ("responses", False),
                 "R1": ("responses", True)}
        if cmd in flags:
            attr, val = flags[cmd]
            setattr(self, attr, val)
            return ok
        if re.fullmatch(r"SPA?[0-9A-C]", cmd):
            self.protocol = int(cmd[-1], 16)
            return ok
        if re.fullmatch(r"AT[0-2]", cmd):
            self.adaptive = int(cmd[2])
            return ok
        m = re.fullmatch(r"ST([0-9A-F]{2})", cmd)
        if m:
            self.st = int(m.group(1), 16)
            return ok
        m = re.fullmatch(r"SH([0-9A-F]{3})", cmd)
        if m:
            self.header = int(m.group(1), 16)
            return ok
        m = re.fullmatch(r"CRA([0-9A-F]{3})", cmd)
        if m:
            self.cra = int(m.group(1), 16)
            return ok
        if cmd == "CRA":
            self.cra = None
            return ok
        if self.bad_fcsm and cmd.startswith("FCS"):
            return ["?"]
        m = re.fullmatch(r"FCSH([0-9A-F]{3})", cmd)
        if m:
            self.fc_header = int(m.group(1), 16)
            return ok
        m = re.fullmatch(r"FCSD((?:[0-9A-F]{2}){1,5})", cmd)
        if m:
            self.fc_data = bytes.fromhex(m.group(1))
            return ok
        m = re.fullmatch(r"FCSM([0-2])", cmd)
        if m:
            mode = int(m.group(1))
            if mode == 1 and (self.fc_header is None or self.fc_data is None):
                return ["?"]
            self.fc_mode = mode
            return ok
        if cmd == "PPS":
            return ["?"]
        return ["?"]

    def _st(self, raw: str) -> list[str]:
        if not self.stn:
            return ["?"]
        up = raw.upper()
        if up.replace(" ", "") == "STI":
            return ["STN2120 v5.6.19 (SIM)"]
        m = re.match(r"STPX\s+(.*)$", up)
        if not m:
            return ["?"]
        params = {}
        for part in m.group(1).split(","):
            if ":" in part:
                k, v = part.split(":", 1)
                params[k.strip()] = v.strip()
        try:
            hdr = int(params["H"], 16)
            data = bytes.fromhex(params["D"])
        except (KeyError, ValueError):
            return ["?"]
        return self._stpx(hdr, data)

    # ---------------------------------------------------------------- the bus
    def _passes(self, can_id: int) -> bool:
        return self.cra is None or can_id == self.cra

    def _fmt(self, can_id: int, data: bytes) -> str:
        sep = " " if self.spaces else ""
        body = sep.join(f"{b:02X}" for b in data)
        if self.headers:
            dlc = f"{len(data)}{sep}" if self.dlc else ""
            return f"{can_id:03X}{sep}{dlc}{body}"
        return body

    def _bus_send(self, can_id: int, data: bytes) -> list[tuple[int, bytes]]:
        """Put one frame on the bus; return (id, frame) replies, auto-FC included."""
        if len(data) < 8:
            data = data + b"\x00" * (8 - len(data))  # adapter pads to 8 bytes
        self.frames_sent.append((can_id, data))
        replies: list[tuple[int, bytes]] = []
        queue = [(can_id, data)]
        while queue:
            cid, frame = queue.pop(0)
            for srv in self.servers:
                for out in srv.on_frame(cid, frame):
                    rid = srv.ecu.rx_id
                    replies.append((rid, out))
                    if self.cfc and out[0] >> 4 == isotp.FF and self._passes(rid):
                        fc = self._auto_fc(rid)
                        if fc is not None:
                            self.frames_sent.append(fc)
                            queue.append(fc)
        return replies

    def _auto_fc(self, rid: int) -> tuple[int, bytes] | None:
        if self.fc_mode == 1 and self.fc_header is not None and self.fc_data is not None:
            data = self.fc_data + b"\x00" * (8 - len(self.fc_data))
            return (self.fc_header, data)
        # mode 0: ISO 15765-4 style — fine for 7E8→7E0, wrong for VAG ids
        return (rid - 8, b"\x30\x00\x00\x00\x00\x00\x00\x00")

    def _collect(self, replies: list[tuple[int, bytes]]) -> list[str]:
        if not self.responses:
            return []
        shown = [(cid, f) for cid, f in replies if self._passes(cid)]
        wait = self.st * 0.004 if self.adaptive == 0 else min(self.st * 0.004, 0.05)
        self.clock.advance(wait)  # adapter keeps listening after the last frame
        if not shown:
            return ["NO DATA"]
        lines = []
        for cid, f in shown:
            if self.headers:
                lines.append(self._fmt(cid, f))
            elif self.caf:
                t = f[0] >> 4
                if t == isotp.SF:
                    lines.append(self._fmt(cid, f[1:1 + (f[0] & 0x0F)]))
                else:
                    lines.append(self._fmt(cid, f))
            else:
                lines.append(self._fmt(cid, f))
        return lines

    def _send_request(self, data: bytes) -> list[str]:
        if self.protocol not in (6, 0):
            return ["CAN ERROR"]
        if self.caf:
            if len(data) > 7:
                return ["?"]
            frame = bytes([len(data)]) + data
        else:
            if len(data) > 8:
                return ["?"]
            frame = data
        return self._collect(self._bus_send(self.header, frame))

    def _stpx(self, hdr: int, payload: bytes) -> list[str]:
        frames = isotp.encode(payload, pad=0x00)
        replies = self._bus_send(hdr, frames[0])
        if len(frames) == 1:
            return self._collect(replies)
        # wait for the FC from the module
        fc_frames = [f for cid, f in replies if f[0] >> 4 == isotp.FC]
        if not fc_frames:
            return ["NO DATA"]
        fc = isotp.parse_flow_control(fc_frames[-1])
        rest = frames[1:]
        collected: list[tuple[int, bytes]] = []
        sent_in_block = 0
        for f in rest:
            out = self._bus_send(hdr, f)
            sent_in_block += 1
            new_fc = [x for _, x in out if x[0] >> 4 == isotp.FC]
            collected += [(c, x) for c, x in out if x[0] >> 4 != isotp.FC]
            if fc.block_size and sent_in_block >= fc.block_size and new_fc:
                fc = isotp.parse_flow_control(new_fc[-1])
                sent_in_block = 0
        return self._collect(collected)
