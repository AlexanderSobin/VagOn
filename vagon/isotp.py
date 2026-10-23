"""ISO 15765-2 (ISO-TP) framing for classic CAN (8-byte frames, normal addressing)."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import IsoTpError

PAD = 0x55          # padding byte used by VW testers
MAX_PAYLOAD = 4095  # 12-bit first-frame length

SF, FF, CF, FC = 0, 1, 2, 3
FC_CTS, FC_WAIT, FC_OVERFLOW = 0, 1, 2


def frame_type(frame: bytes) -> int:
    if not frame:
        raise IsoTpError("пустой CAN-кадр")
    return frame[0] >> 4


def _pad(frame: bytes, pad: int | None) -> bytes:
    if pad is None or len(frame) >= 8:
        return frame
    return frame + bytes([pad]) * (8 - len(frame))


def encode(payload: bytes, pad: int | None = PAD) -> list[bytes]:
    """Split a UDS payload into ISO-TP frames: [SF] or [FF, CF1, CF2, ...]."""
    n = len(payload)
    if n == 0:
        raise IsoTpError("пустое сообщение")
    if n > MAX_PAYLOAD:
        raise IsoTpError(f"сообщение длиннее {MAX_PAYLOAD} байт")
    if n <= 7:
        return [_pad(bytes([n]) + payload, pad)]
    frames = [bytes([0x10 | (n >> 8), n & 0xFF]) + payload[:6]]
    sn = 1
    for i in range(6, n, 7):
        frames.append(_pad(bytes([0x20 | sn]) + payload[i:i + 7], pad))
        sn = (sn + 1) & 0x0F
    return frames


def flow_control(status: int = FC_CTS, block_size: int = 0, st_min: int = 0,
                 pad: int | None = PAD) -> bytes:
    return _pad(bytes([0x30 | status, block_size, st_min]), pad)


@dataclass
class FlowControl:
    status: int
    block_size: int
    st_min_raw: int

    @property
    def st_min_s(self) -> float:
        """Minimum separation time in seconds (ISO 15765-2 encoding)."""
        v = self.st_min_raw
        if v <= 0x7F:
            return v / 1000.0
        if 0xF1 <= v <= 0xF9:
            return (v - 0xF0) / 10000.0
        return 0.127  # reserved values: treat as the maximum


def parse_flow_control(frame: bytes) -> FlowControl:
    if len(frame) < 3 or frame_type(frame) != FC:
        raise IsoTpError(f"ожидался кадр Flow Control, получен {frame.hex(' ')}")
    status = frame[0] & 0x0F
    if status > 2:
        raise IsoTpError(f"неверный статус Flow Control: {status}")
    return FlowControl(status, frame[1], frame[2])


class Reassembler:
    """Collects frames from one sender into complete messages.

    feed() returns a complete payload when a message is finished, None while a
    multi-frame message is in progress (or for a frame that carries no data, such
    as a flow-control frame, which is reported via .last_flow_control).
    """

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._buf = bytearray()
        self._expected = 0
        self._next_sn = 1
        self.in_progress = False
        self.last_flow_control: FlowControl | None = None

    def feed(self, frame: bytes) -> bytes | None:
        t = frame_type(frame)
        if t == SF:
            n = frame[0] & 0x0F
            if n == 0:
                # CAN-FD escape (length in byte 1) — not used on classic CAN
                if len(frame) < 2 or frame[1] == 0 or len(frame) < 2 + frame[1]:
                    raise IsoTpError(f"неверный Single Frame: {frame.hex(' ')}")
                n = frame[1]
                data = bytes(frame[2:2 + n])
            else:
                if len(frame) < 1 + n:
                    raise IsoTpError(f"Single Frame короче заявленного: {frame.hex(' ')}")
                data = bytes(frame[1:1 + n])
            self.reset()
            return data
        if t == FF:
            if len(frame) < 8:
                raise IsoTpError(f"First Frame короче 8 байт: {frame.hex(' ')}")
            n = ((frame[0] & 0x0F) << 8) | frame[1]
            if n < 8:
                raise IsoTpError(f"First Frame с длиной {n} < 8")
            self.reset()
            self._expected = n
            self._buf += frame[2:8]
            self.in_progress = True
            return None
        if t == CF:
            if not self.in_progress:
                raise IsoTpError(f"Consecutive Frame без First Frame: {frame.hex(' ')}")
            sn = frame[0] & 0x0F
            if sn != self._next_sn:
                self.reset()
                raise IsoTpError(f"потерян кадр ISO-TP: ожидался SN {self._next_sn}, пришёл {sn}")
            self._next_sn = (self._next_sn + 1) & 0x0F
            self._buf += frame[1:8]
            if len(self._buf) >= self._expected:
                data = bytes(self._buf[:self._expected])
                self.reset()
                return data
            return None
        if t == FC:
            self.last_flow_control = parse_flow_control(frame)
            return None
        raise IsoTpError(f"неизвестный тип кадра ISO-TP: {frame.hex(' ')}")

    @property
    def remaining(self) -> int:
        return max(0, self._expected - len(self._buf)) if self.in_progress else 0
