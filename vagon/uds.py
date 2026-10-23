"""UDS (ISO 14229-1) client on top of a transport Channel."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .errors import NegativeResponse, ResponsePendingTimeout, SafetyError, TimeoutError_, VagOnError
from .hexutil import to_hex
from .transport.base import Channel

log = logging.getLogger("vagon.uds")

# Services
DIAGNOSTIC_SESSION_CONTROL = 0x10
ECU_RESET = 0x11
CLEAR_DTC = 0x14
READ_DTC = 0x19
READ_DID = 0x22
SECURITY_ACCESS = 0x27
WRITE_DID = 0x2E
ROUTINE_CONTROL = 0x31
TESTER_PRESENT = 0x3E

# Sessions
SESSION_DEFAULT = 0x01
SESSION_PROGRAMMING = 0x02  # never used by VagOn (flashing is out of scope)
SESSION_EXTENDED = 0x03

NRC_PENDING = 0x78
NRC_BUSY = 0x21

# Never sent by VagOn: flashing / memory writes / bus control. A request for
# any of these is refused before it reaches the adapter.
FORBIDDEN_SIDS = {
    0x28: "CommunicationControl",
    0x34: "RequestDownload", 0x35: "RequestUpload", 0x36: "TransferData",
    0x37: "RequestTransferExit", 0x38: "RequestFileTransfer", 0x3D: "WriteMemoryByAddress",
    0x87: "LinkControl",
}
ALLOWED_SESSIONS = {SESSION_DEFAULT, SESSION_EXTENDED, 0x4F}

# How many request bytes after the SID a positive response echoes (sub-function / identifier).
_ECHO_LEN = {READ_DID: 2, WRITE_DID: 2, DIAGNOSTIC_SESSION_CONTROL: 1, SECURITY_ACCESS: 1,
             ROUTINE_CONTROL: 3, READ_DTC: 1, ECU_RESET: 1, TESTER_PRESENT: 1}


def check_policy(payload: bytes) -> None:
    if not payload:
        raise SafetyError("пустой запрос")
    sid = payload[0]
    if sid in FORBIDDEN_SIDS:
        raise SafetyError(f"VagOn никогда не отправляет {FORBIDDEN_SIDS[sid]} (0x{sid:02X}) — прошивка и "
                          "запись памяти запрещены")
    if sid == DIAGNOSTIC_SESSION_CONTROL and len(payload) >= 2 and (payload[1] & 0x7F) not in ALLOWED_SESSIONS:
        raise SafetyError(f"сессия 0x{payload[1] & 0x7F:02X} запрещена (разрешены 0x01, 0x03, 0x4F)")


@dataclass
class SessionTiming:
    p2: float = 0.05
    p2_star: float = 5.0


class UdsClient:
    """Minimal, strict UDS client.

    Every method returns the *data* part of the positive response (after the
    echoed SID and identifiers) and raises NegativeResponse on 0x7F.
    """

    def __init__(self, channel: Channel, *, timeout: float = 2.0, pending_timeout: float = 6.0,
                 busy_retries: int = 8, max_pending: int = 30):
        self.ch = channel
        self.timeout = timeout
        self.pending_timeout = pending_timeout
        self.busy_retries = busy_retries
        self.max_pending = max_pending
        self.session = SESSION_DEFAULT
        self.timing = SessionTiming()
        self.last_activity = 0.0

    # ------------------------------------------------------------------ core
    def request(self, payload: bytes, *, timeout: float | None = None) -> bytes:
        """Send `payload`, return the full positive response (starting with SID+0x40)."""
        check_policy(payload)
        busy_left = self.busy_retries
        while True:
            try:
                return self._request_once(payload, timeout or self.timeout)
            except NegativeResponse as e:
                if e.nrc == NRC_BUSY and busy_left > 0:
                    busy_left -= 1
                    time.sleep(0.3)
                    continue
                raise
            finally:
                self.last_activity = time.monotonic()

    def _request_once(self, payload: bytes, timeout: float) -> bytes:
        sid = payload[0]
        log.debug("→ %03X %s", self.ch.tx_id, to_hex(payload))
        responses = self.ch.exchange(payload, timeout)
        pending = 0
        deadline = None
        while True:
            for resp in responses:
                log.debug("← %03X %s", self.ch.rx_id, to_hex(resp))
                if not resp:
                    continue
                if resp[0] == 0x7F and len(resp) >= 3 and resp[1] == sid:
                    if resp[2] == NRC_PENDING:
                        pending += 1
                        if pending > self.max_pending:
                            raise ResponsePendingTimeout(
                                f"блок 0x{self.ch.tx_id:03X} слишком долго отвечает 'ожидайте'")
                        deadline = time.monotonic() + self.pending_timeout
                        continue
                    raise NegativeResponse(sid, resp[2], payload)
                if resp[0] == (sid + 0x40) & 0xFF:
                    n = _ECHO_LEN.get(sid, 0)
                    if resp[1:1 + n] == payload[1:1 + n]:
                        return resp
                    # same service, other identifier: a late answer to an earlier request
                    log.debug("ответ на другой идентификатор пропущен: %s", to_hex(resp))
                    continue
                # anything else is a stray reply (e.g. late answer to a previous request)
                log.debug("игнорирую посторонний ответ %s", to_hex(resp))
            if pending and deadline is not None and time.monotonic() < deadline:
                responses = self.ch.listen(max(0.1, deadline - time.monotonic()))
                if responses:
                    continue
                raise ResponsePendingTimeout(
                    f"блок 0x{self.ch.tx_id:03X} ответил 'ожидайте' (0x78), но итоговый ответ "
                    "не дошёл через адаптер. Проверьте результат чтением.")
            if pending:
                raise ResponsePendingTimeout(
                    f"блок 0x{self.ch.tx_id:03X}: итоговый ответ после 0x78 не получен")
            raise TimeoutError_(f"нет ответа от блока 0x{self.ch.tx_id:03X} на {to_hex(payload[:3])}")

    # -------------------------------------------------------------- services
    def diagnostic_session(self, session: int) -> bytes:
        resp = self.request(bytes([DIAGNOSTIC_SESSION_CONTROL, session]))
        if len(resp) < 2 or resp[1] != session:
            raise VagOnError(f"неожиданный ответ на смену сессии: {to_hex(resp)}")
        if len(resp) >= 6:
            p2 = int.from_bytes(resp[2:4], "big") / 1000.0
            p2s = int.from_bytes(resp[4:6], "big") * 10 / 1000.0
            self.timing = SessionTiming(p2, p2s)
            self.pending_timeout = max(self.pending_timeout, min(p2s + 1.0, 30.0))
        self.session = session
        return resp[2:]

    def tester_present(self) -> None:
        self.request(bytes([TESTER_PRESENT, 0x00]))

    def read_did(self, did: int) -> bytes:
        resp = self.request(bytes([READ_DID, did >> 8, did & 0xFF]))
        if len(resp) < 3 or ((resp[1] << 8) | resp[2]) != did:
            raise VagOnError(f"ответ на чтение 0x{did:04X} с чужим идентификатором: {to_hex(resp)}")
        return resp[3:]

    def write_did(self, did: int, data: bytes) -> None:
        resp = self.request(bytes([WRITE_DID, did >> 8, did & 0xFF]) + bytes(data),
                            timeout=max(self.timeout, 3.0))
        if len(resp) < 3 or ((resp[1] << 8) | resp[2]) != did:
            raise VagOnError(f"неожиданный ответ на запись 0x{did:04X}: {to_hex(resp)}")

    def request_seed(self, level: int) -> bytes:
        if level % 2 == 0:
            raise ValueError("уровень запроса seed должен быть нечётным")
        resp = self.request(bytes([SECURITY_ACCESS, level]))
        if len(resp) < 2 or resp[1] != level:
            raise VagOnError(f"неожиданный ответ на запрос seed: {to_hex(resp)}")
        return resp[2:]

    def send_key(self, level: int, key: bytes) -> None:
        resp = self.request(bytes([SECURITY_ACCESS, level + 1]) + bytes(key))
        if len(resp) < 2 or resp[1] != level + 1:
            raise VagOnError(f"неожиданный ответ на ключ: {to_hex(resp)}")

    def routine(self, control: int, rid: int, data: bytes = b"") -> bytes:
        resp = self.request(bytes([ROUTINE_CONTROL, control, rid >> 8, rid & 0xFF]) + data,
                            timeout=max(self.timeout, 3.0))
        return resp[4:]

    def read_dtc_by_status(self, mask: int = 0xFF) -> tuple[int, list[tuple[int, int]]]:
        """Return (availability mask, [(dtc 24-bit, status), ...])."""
        resp = self.request(bytes([READ_DTC, 0x02, mask]), timeout=max(self.timeout, 3.0))
        if len(resp) < 3:
            raise VagOnError(f"короткий ответ DTC: {to_hex(resp)}")
        avail = resp[2]
        body = resp[3:]
        dtcs = []
        for i in range(0, len(body) - len(body) % 4, 4):
            code = (body[i] << 16) | (body[i + 1] << 8) | body[i + 2]
            dtcs.append((code, body[i + 3]))
        return avail, dtcs

    def clear_dtc(self, group: int = 0xFFFFFF) -> None:
        self.request(bytes([CLEAR_DTC, (group >> 16) & 0xFF, (group >> 8) & 0xFF, group & 0xFF]),
                     timeout=max(self.timeout, 5.0))

    def ecu_reset(self, kind: int = 0x01) -> None:
        self.request(bytes([ECU_RESET, kind]))
