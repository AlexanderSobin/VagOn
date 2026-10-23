"""ELM327 / STN11xx / STN2xxx driver.

Design notes (each one is a real-car pitfall):

* Headers + spaces are ON (ATH1 ATS1), so every received frame is printed raw
  with its CAN ID and ISO-TP PCI byte; VagOn reassembles ISO-TP itself.
* Flow control for multi-frame *responses* is sent by the adapter, but its
  default (mode 0) derives the FC CAN ID from the response ID, which is wrong
  for VAG IDs (0x778 → FC would go to some other module). VagOn sets
  ATFCSH <request id> / ATFCSD 30 00 <STmin> / ATFCSM1 for every target.
* With CAN auto-formatting the ELM327 can only *send* single frames
  (≤7 payload bytes). Long-coding writes are longer, so VagOn sends
  multi-frame requests by hand (ATCAF0: First Frame, wait for the module's
  Flow Control, ATR0 for the consecutive frames, ATR1 for the last one).
  STN chips have STPX for this and it is used when available.
* STmin pacing (default 10 ms) keeps the adapter's 512-byte buffer from
  overflowing ('BUFFER FULL') on slow internal UARTs of cheap clones.
* Echo stays on and is verified, so a misheard command is detected.
* A bare CR repeats the previous command on an ELM327; VagOn never sends one.
"""

from __future__ import annotations

import logging
import re
import time

from .. import isotp
from ..errors import AdapterError, EchoMismatch, IsoTpError, TimeoutError_, TransportError
from ..hexutil import to_hex
from .base import Channel, Link, TraceHook, Transport

log = logging.getLogger("vagon.elm")

PROMPT = b">"

_STATUS_FATAL = (
    "CAN ERROR", "BUS ERROR", "BUFFER FULL", "BUS BUSY", "FB ERROR", "DATA ERROR",
    "RX ERROR", "LV RESET", "ACT ALERT", "LP ALERT", "UNABLE TO CONNECT", "STOPPED",
)
_STATUS_IGNORABLE = ("SEARCHING", "BUS INIT", "OK")

_FRAME_11 = re.compile(r"^([0-9A-F]{3})\s?((?:[0-9A-F]{2}\s?){1,8})$")
_FRAME_29 = re.compile(r"^([0-9A-F]{2}\s?[0-9A-F]{2}\s?[0-9A-F]{2}\s?[0-9A-F]{2})\s((?:[0-9A-F]{2}\s?){1,8})$")


class Elm327(Transport):
    """Drives an ELM327-compatible adapter over any Link."""

    def __init__(self, link: Link, *, trace: TraceHook | None = None, st_min_ms: int = 10,
                 at_timeout: float = 3.0, use_stpx: bool | None = None):
        self.link = link
        self.trace = trace
        self.st_min_ms = max(0, min(127, int(st_min_ms)))
        self.at_timeout = at_timeout
        self.use_stpx = use_stpx
        self.description = f"ELM327 via {link.name}"
        self.version = ""
        self.device = ""
        self.is_stn = False
        self.voltage: float | None = None
        self._rxbuf = bytearray()
        self._tx_id: int | None = None
        self._rx_id: int | None = None
        self._caf = True
        self._resp_on = True
        self._adaptive = True
        self._st = 0xFF
        self._cra_supported = True
        self._fc_ok = True
        self._fc_warned = False
        self._cfc_off = False
        self._broken: str | None = None
        self._echo: bool | None = None     # does this adapter echo? (None while probing)
        self._last_had_echo = False
        self._connected = False

    # ---------------------------------------------------------------- tracing
    def _t(self, direction: str, text: str) -> None:
        if self.trace:
            try:
                self.trace(direction, text)
            except Exception:  # tracing must never break diagnostics
                pass
        log.debug("%s %s", direction, text)

    # --------------------------------------------------------------- low level
    def _write(self, text: str) -> None:
        if self._broken:
            raise TransportError(f"адаптер в неизвестном состоянии ({self._broken}); переподключитесь")
        if not text or "\r" in text:
            raise ValueError("пустая или многострочная команда")
        self._t(">>", text)
        self.link.write(text.encode("ascii") + b"\r")

    def _read_until_prompt(self, silence: float, total: float) -> str:
        """Read until the '>' prompt. `silence`: max quiet gap, `total`: overall cap."""
        start = time.monotonic()
        last = start
        while True:
            idx = self._rxbuf.find(PROMPT)
            if idx >= 0:
                raw = bytes(self._rxbuf[:idx])
                del self._rxbuf[:idx + 1]
                text = raw.replace(b"\x00", b"").decode("ascii", "replace")
                self._t("<<", text.replace("\r", "\\r").replace("\n", "\\n"))
                return text
            now = time.monotonic()
            if now - last > silence or now - start > total:
                self._broken = "нет приглашения '>' от адаптера"
                raise TimeoutError_(
                    "адаптер не ответил (нет '>'). Проверьте связь/питание адаптера и зажигание.")
            chunk = self.link.read(0.05)
            if chunk:
                self._rxbuf += chunk
                last = time.monotonic()

    @staticmethod
    def _strip_prefix(line: str, sent: str) -> str | None:
        """If `line` starts with `sent` (ignoring spaces/case), return the rest, else None."""
        want = sent.replace(" ", "").upper()
        i = n = 0
        up = line.upper()
        while n < len(want) and i < len(up):
            if up[i] == " ":
                i += 1
                continue
            if up[i] != want[n]:
                return None
            i += 1
            n += 1
        return line[i:].strip() if n == len(want) else None

    def _lines(self, text: str, sent: str | None) -> list[str]:
        """Split an answer into lines and remove (and verify) the echo of `sent`."""
        lines = [ln.strip() for ln in re.split(r"[\r\n]+", text)]
        lines = [ln for ln in lines if ln]
        if sent is None or self._echo is False:
            return lines
        rest = self._strip_prefix(lines[0], sent) if lines else None
        self._last_had_echo = rest is not None
        if rest is not None:
            # exact echo, or echo glued to the answer (clones that omit the CR)
            return ([rest] if rest else []) + lines[1:]
        if self._echo is None:
            return lines            # still probing whether this adapter echoes at all
        heard = lines[0] if lines else "(ничего)"
        is_request = all(c in "0123456789ABCDEFabcdef" for c in sent)
        if not is_request:
            # a misheard AT command may have changed any setting: state unknown
            self._broken = f"эхо команды {sent} не совпало"
            self._tx_id = self._rx_id = None
        raise EchoMismatch(sent, heard)

    def _cmd(self, cmd: str, *, timeout: float | None = None, expect_ok: bool = True,
             allow_unknown: bool = False) -> list[str]:
        """Send an AT/ST command; return answer lines (echo stripped)."""
        self._check_idle()
        self._write(cmd)
        t = timeout or self.at_timeout
        lines = self._lines(self._read_until_prompt(silence=t, total=t + 2.0), cmd)
        if any(ln == "?" for ln in lines):
            if allow_unknown:
                return ["?"]
            raise AdapterError(f"адаптер не понимает команду {cmd} — нужен полноценный ELM327 v1.4+")
        if expect_ok and not any(ln.endswith("OK") for ln in lines):
            raise AdapterError(f"адаптер ответил на {cmd}: {' | '.join(lines) or '(пусто)'}")
        return lines

    def _check_idle(self) -> None:
        """Anything that arrives between a prompt and our next command means trouble."""
        stray = self.link.read(0.0) if hasattr(self.link, "read") else b""
        if stray:
            self._rxbuf += stray
        if self._rxbuf.strip(b"\x00\r\n "):
            junk = bytes(self._rxbuf)
            self._rxbuf.clear()
            text = junk.decode("ascii", "replace")
            self._t("<<!", text)
            if "ELM" in text or "RESET" in text or "STN" in text:
                self._broken = "адаптер перезагрузился"
                self._tx_id = self._rx_id = None
                raise TransportError("адаптер перезагрузился (просадка питания?) — переподключитесь")
        else:
            self._rxbuf.clear()

    # ----------------------------------------------------------------- connect
    def connect(self) -> "Elm327":
        self.link.drain(quiet=0.2, limit=1.0)
        self._rxbuf.clear()
        # ATZ resets everything; banner + prompt follow (~1 s).
        self._write("ATZ")
        text = ""
        for _ in range(3):
            try:
                text = self._read_until_prompt(silence=3.0, total=6.0)
                break
            except TimeoutError_:
                self._broken = None
                self._write("ATI")
        lines = self._lines(text, "ATZ")
        banner = next((ln for ln in lines if "ELM" in ln.upper() or "STN" in ln.upper()
                       or "OBD" in ln.upper()), "")
        if not banner:
            # some BLE clones stay silent on ATZ; ask for the ID
            ident = self._cmd("ATI", expect_ok=False)
            banner = next((ln for ln in ident if ln), "")
        if not banner:
            raise AdapterError("это не похоже на ELM327: нет ответа на ATZ/ATI")
        self.version = banner
        self._broken = None
        self._echo = None
        # Explicit settings (don't rely on factory defaults / programmable params).
        # Echo stays ON and is verified for every later line.
        self._cmd("ATE1")
        self._cmd("ATI", expect_ok=False)       # echo is certainly on now: was "ATI" repeated?
        self._echo = self._last_had_echo
        if not self._echo:
            log.warning("адаптер не повторяет команды (нет эха) — проверка эха отключена")
        for cmd in ("ATL0", "ATS1", "ATH1"):
            self._cmd(cmd)
        self._cmd("ATD0", allow_unknown=True)  # no DLC display (v1.3+)
        self._cmd("ATCAF1")
        self._cmd("ATCFC1")
        self._cmd("ATSP6")      # ISO 15765-4 CAN, 11 bit, 500 kbit/s — no auto search
        self._cmd("ATAT1")
        self._cmd("ATSTFF")     # max wait ~1 s for each frame
        self._cmd("ATR1")
        self._caf, self._resp_on, self._adaptive, self._st = True, True, True, 0xFF
        sti = self._cmd("STI", expect_ok=False, allow_unknown=True)
        self.is_stn = bool(sti and sti[0] != "?" and "STN" in sti[0].upper())
        if self.use_stpx is None:
            self.use_stpx = self.is_stn
        dev = self._cmd("AT@1", expect_ok=False, allow_unknown=True)
        self.device = dev[0] if dev and dev[0] != "?" else ""
        try:
            rv = self._cmd("ATRV", expect_ok=False, allow_unknown=True)
            m = re.search(r"(\d+(?:\.\d+)?)\s*V", " ".join(rv))
            self.voltage = float(m.group(1)) if m else None
        except AdapterError:
            self.voltage = None
        self._tx_id = self._rx_id = None
        self._connected = True
        self.description = f"{self.version}{' (STN)' if self.is_stn else ''} via {self.link.name}"
        return self

    def info(self) -> dict:
        return {"description": self.description, "version": self.version, "device": self.device,
                "stn": self.is_stn, "voltage": self.voltage, "link": self.link.name}

    def read_voltage(self) -> float | None:
        rv = self._cmd("ATRV", expect_ok=False, allow_unknown=True)
        m = re.search(r"(\d+(?:\.\d+)?)\s*V", " ".join(rv))
        self.voltage = float(m.group(1)) if m else None
        return self.voltage

    def close(self) -> None:
        try:
            self.link.close()
        finally:
            self._connected = False

    # ------------------------------------------------------------------ target
    def _set_target(self, tx_id: int, rx_id: int) -> None:
        if self._tx_id == tx_id and self._rx_id == rx_id:
            return
        if not (0 <= tx_id <= 0x7FF and 0 <= rx_id <= 0x7FF):
            raise ValueError("поддерживаются только 11-битные CAN ID")
        self._tx_id = self._rx_id = None
        self._cmd(f"ATSH{tx_id:03X}")
        if self._cra_supported:
            res = self._cmd(f"ATCRA{rx_id:03X}", allow_unknown=True)
            if res == ["?"]:
                self._cra_supported = False
        if not self._cra_supported:
            self._cmd(f"ATCF{rx_id:03X}")
            self._cmd("ATCM7FF")
        fc_ok = True
        for cmd in (f"ATFCSH{tx_id:03X}", f"ATFCSD3000{self.st_min_ms:02X}", "ATFCSM1"):
            if self._cmd(cmd, allow_unknown=True) == ["?"]:
                fc_ok = False
                break
        if not fc_ok and not self._fc_warned:
            self._fc_warned = True
            log.warning("адаптер не поддерживает ATFCSM1: VagOn сам отвечает Flow Control (медленнее)")
        if not fc_ok and not self._cfc_off:
            # the adapter's own FC would go to (response id - 8) — another module; switch it off
            # and answer First Frames ourselves (see _request_manual_fc)
            self._cfc_off = self._cmd("ATCFC0", allow_unknown=True) != ["?"]
        self._fc_ok = fc_ok
        self._tx_id, self._rx_id = tx_id, rx_id

    def _set_caf(self, on: bool) -> None:
        if self._caf != on:
            self._cmd("ATCAF1" if on else "ATCAF0")
            self._caf = on

    def _set_resp(self, on: bool) -> None:
        if self._resp_on != on:
            self._cmd("ATR1" if on else "ATR0")
            self._resp_on = on

    def set_patient(self, patient: bool) -> None:
        """Turn adaptive timing off for slow operations (writes, logins)."""
        want_adaptive = not patient
        if self._adaptive != want_adaptive:
            self._cmd("ATAT1" if want_adaptive else "ATAT0")
            self._adaptive = want_adaptive

    def fast_timeouts(self, on: bool) -> None:
        """Short per-frame timeout for module scans (absent modules answer nothing)."""
        self._set_st(0x32 if on else 0xFF)

    def _set_st(self, value: int) -> None:
        if self._st != value:
            self._cmd(f"ATST{value:02X}")
            self._st = value

    # ---------------------------------------------------------------- requests
    def _raw_request(self, hexline: str, timeout: float) -> list[str]:
        """Send a line of hex digits (a CAN payload), return answer lines."""
        self._check_idle()
        self._write(hexline)
        lines = self._lines(self._read_until_prompt(silence=timeout + 1.5, total=timeout + 60.0),
                            hexline)
        return lines

    def _parse_frames(self, lines: list[str], rx_id: int) -> list[bytes]:
        frames: list[bytes] = []
        for ln in lines:
            up = ln.upper()
            if up == "NO DATA" or up.startswith("NO DATA"):
                continue
            if any(up.startswith(s) for s in _STATUS_IGNORABLE):
                continue
            if "<" in up or any(s in up for s in _STATUS_FATAL) or up.startswith("ERR") \
                    or "ELM327" in up or up.startswith("STN"):
                if any(x in up for x in ("LV RESET", "ELM327", "ACT ALERT", "LP ALERT", "STOPPED")) \
                        or up.startswith("STN") or up.startswith("ERR9"):
                    # the adapter reset or was interrupted: every setting is back to defaults
                    self._broken = f"адаптер сообщил «{ln}»"
                    self._tx_id = self._rx_id = None
                    raise TransportError(f"адаптер перезагрузился/прервался («{ln}») — просадка питания? "
                                         "Переподключитесь; VagOn проверит результат записи чтением.")
                if "BUFFER FULL" in up:
                    raise AdapterError("BUFFER FULL: адаптер не успевает — увеличьте STmin (--stmin 20)")
                if "CAN ERROR" in up or "BUS ERROR" in up:
                    raise AdapterError(
                        "CAN ERROR: нет связи с шиной. Включите зажигание, проверьте посадку адаптера.")
                raise AdapterError(f"адаптер сообщил: {ln}")
            if up == "?":
                raise AdapterError("адаптер не понял запрос")
            m = _FRAME_11.match(up)
            if m:
                can_id = int(m.group(1), 16)
                data = bytes.fromhex(m.group(2).replace(" ", ""))
                if can_id != rx_id:
                    log.debug("кадр с чужим ID %03X проигнорирован", can_id)
                    continue
                frames.append(data)
                continue
            if _FRAME_29.match(up):
                continue
            # DLC display variant: "778 8 03 62 ..."
            parts = up.split()
            if len(parts) >= 3 and len(parts[0]) == 3 and len(parts[1]) == 1:
                try:
                    can_id = int(parts[0], 16)
                    data = bytes.fromhex("".join(parts[2:]))
                except ValueError:
                    raise AdapterError(f"непонятная строка от адаптера: {ln}") from None
                if can_id == rx_id:
                    frames.append(data)
                continue
            raise AdapterError(f"непонятная строка от адаптера: {ln}")
        return frames

    def _reassemble(self, frames: list[bytes]) -> list[bytes]:
        r = isotp.Reassembler()
        out = []
        for f in frames:
            if isotp.frame_type(f) == isotp.FC:
                continue
            msg = r.feed(f)
            if msg is not None:
                out.append(msg)
        if r.in_progress:
            raise IsoTpError("ответ блока оборван (не все кадры дошли до адаптера)")
        return out

    @staticmethod
    def _needs_fc(frames: list[bytes]) -> bool:
        """True if the frames end inside a multi-frame message (First Frame not completed)."""
        r = isotp.Reassembler()
        for f in frames:
            if isotp.frame_type(f) != isotp.FC:
                r.feed(f)
        return r.in_progress

    # Services whose positive answer may need several frames (reads, DTC lists). Only these use
    # the short-listen manual flow-control path; writes, sessions and logins keep ATSTFF so a
    # slow module answer is never missed.
    MULTIFRAME_ANSWER_SIDS = {0x22, 0x19, 0x1A}

    def _request_manual_fc(self, rx_id: int, payload: bytes, timeout: float) -> list[bytes]:
        """Single-frame request on an adapter without ATFCSM1: answer the module's First
        Frame with our own Flow Control frame (sent raw to the module's request id).

        The module waits up to N_Bs (~1 s) for flow control, so the adapter may only
        listen briefly after the First Frame (ATST32 = 200 ms) before printing '>'.
        """
        prev_adaptive = self._adaptive
        try:
            self.set_patient(True)
            self._set_st(self.FC_LISTEN_ST)
            self._set_caf(True)
            frames = self._parse_frames(self._raw_request(payload.hex().upper(), timeout), rx_id)
            if not frames:
                # nothing within 200 ms: a slow module — ask again patiently (reads are idempotent)
                self._set_st(0xFF)
                frames = self._parse_frames(self._raw_request(payload.hex().upper(), timeout), rx_id)
            if self._needs_fc(frames):
                self._set_caf(False)
                fc = isotp.flow_control(isotp.FC_CTS, 0, self.st_min_ms, pad=isotp.PAD)
                frames += self._parse_frames(self._raw_request(fc.hex().upper(), timeout), rx_id)
            return self._reassemble(frames)
        finally:
            try:
                self._set_caf(True)
                self._set_st(0xFF)
                self.set_patient(not prev_adaptive)
            except TransportError:
                pass

    def request(self, tx_id: int, rx_id: int, payload: bytes, timeout: float) -> list[bytes]:
        if not self._connected:
            raise TransportError("адаптер не подключён")
        self._set_target(tx_id, rx_id)
        if len(payload) <= 7:
            if not self._fc_ok and payload[0] in self.MULTIFRAME_ANSWER_SIDS:
                return self._request_manual_fc(rx_id, payload, timeout)
            self._set_caf(True)
            lines = self._raw_request(payload.hex().upper(), timeout)
            return self._reassemble(self._parse_frames(lines, rx_id))
        if self.use_stpx:
            return self._request_stpx(tx_id, rx_id, payload, timeout)
        return self._request_manual_multiframe(rx_id, payload, timeout)

    def _request_stpx(self, tx_id: int, rx_id: int, payload: bytes, timeout: float) -> list[bytes]:
        self._set_caf(True)
        t_ms = max(1, min(65535, int(timeout * 1000)))
        cmd = f"STPX h:{tx_id:03X}, d:{payload.hex().upper()}, r:1, t:{t_ms}"
        self._check_idle()
        self._write(cmd)
        lines = self._lines(self._read_until_prompt(silence=timeout + 2.0, total=timeout + 30.0), cmd)
        if lines == ["?"]:
            self.use_stpx = False
            return self._request_manual_multiframe(rx_id, payload, timeout)
        return self._reassemble(self._parse_frames(lines, rx_id))

    # Wait after a Flow Control frame before the adapter prints '>'. Must stay well
    # below the module's N_Cr (1 s) because the next consecutive frame can only be
    # sent after the prompt: ATST32 = 50 x 4 ms = 200 ms with adaptive timing off.
    FC_LISTEN_ST = 0x32

    def _request_manual_multiframe(self, rx_id: int, payload: bytes, timeout: float) -> list[bytes]:
        """Send a >7 byte request frame by frame (works on every ELM327 clone)."""
        frames = isotp.encode(payload, pad=isotp.PAD)
        prev_adaptive = self._adaptive
        self._set_caf(False)
        try:
            self.set_patient(True)              # exact timeouts, no adaptive guessing
            self._set_st(self.FC_LISTEN_ST)
            self._set_resp(True)
            lines = self._raw_request(frames[0].hex().upper(), timeout)
            fc = self._expect_fc(lines, rx_id)
            idx = 1
            in_block = 0
            last_tx = time.monotonic()
            answer: list[str] = []
            while idx < len(frames):
                is_last = idx == len(frames) - 1
                block_end = bool(fc.block_size) and (in_block + 1) == fc.block_size and not is_last
                if is_last:
                    self._set_st(0xFF)          # the real answer may take a while
                    self._set_resp(True)
                elif block_end:
                    self._set_st(self.FC_LISTEN_ST)
                    self._set_resp(True)
                else:
                    self._set_resp(False)       # don't listen between consecutive frames
                wait = fc.st_min_s - (time.monotonic() - last_tx)
                if wait > 0:
                    time.sleep(wait)
                lines = self._raw_request(frames[idx].hex().upper(), timeout)
                last_tx = time.monotonic()
                idx += 1
                in_block += 1
                if is_last:
                    answer = lines
                elif block_end:
                    fc = self._expect_fc(lines, rx_id)
                    in_block = 0
                elif lines:
                    # with ATR0 nothing should come back; parse to surface adapter errors
                    self._parse_frames(lines, rx_id)
            return self._reassemble(self._parse_frames(answer, rx_id))
        finally:
            try:
                self._set_resp(True)
                self._set_st(0xFF)
                self._set_caf(True)
                self.set_patient(not prev_adaptive)
            except TransportError:
                pass

    def _expect_fc(self, lines: list[str], rx_id: int) -> isotp.FlowControl:
        frames = self._parse_frames(lines, rx_id)
        for f in frames:
            if isotp.frame_type(f) == isotp.FC:
                fc = isotp.parse_flow_control(f)
                if fc.status == isotp.FC_CTS:
                    return fc
                if fc.status == isotp.FC_OVERFLOW:
                    raise IsoTpError("блок сообщил переполнение буфера (запрос слишком длинный)")
                raise IsoTpError("блок попросил подождать (FC WAIT) — повторите позже")
            if isotp.frame_type(f) == isotp.SF and len(f) > 1 and f[1] == 0x7F:
                # module refused right after the First Frame
                raise IsoTpError(f"блок отверг начало запроса: {to_hex(f)}")
        raise TimeoutError_("блок не прислал Flow Control на первый кадр (нет связи/неверный ID)")

    def channel(self, tx_id: int, rx_id: int) -> "ElmChannel":
        return ElmChannel(self, tx_id, rx_id)


class ElmChannel(Channel):
    def __init__(self, elm: Elm327, tx_id: int, rx_id: int):
        self.elm = elm
        self.tx_id = tx_id
        self.rx_id = rx_id

    def exchange(self, payload: bytes, timeout: float) -> list[bytes]:
        return self.elm.request(self.tx_id, self.rx_id, payload, timeout)

    def listen(self, timeout: float) -> list[bytes]:
        # A plain ELM327 cannot listen without sending; the caller verifies by reading back.
        return []
