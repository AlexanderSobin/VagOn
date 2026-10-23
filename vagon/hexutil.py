"""Hex helpers that are strict about their input."""

from __future__ import annotations

import string

_HEX = set(string.hexdigits)


def parse_hex(text: str) -> bytes:
    """Parse '0A 1b', '0a1b', '0x0A,0x1B' into bytes. Rejects odd lengths and junk."""
    if text is None:
        raise ValueError("пустая hex-строка")
    cleaned = text.strip()
    for sep in (" ", ",", ":", "-", "\t", "\n", "\r"):
        cleaned = cleaned.replace(sep, " ")
    parts = [p for p in cleaned.split(" ") if p]
    out = bytearray()
    for part in parts:
        if part.lower().startswith("0x"):
            part = part[2:]
        if not part or any(c not in _HEX for c in part):
            raise ValueError(f"не hex: {part!r}")
        if len(part) % 2:
            if len(parts) > 1 and len(part) == 1:
                part = "0" + part
            else:
                raise ValueError(f"нечётное число hex-цифр: {part!r}")
        out += bytes.fromhex(part)
    if not out:
        raise ValueError("пустая hex-строка")
    return bytes(out)


def to_hex(data: bytes, sep: str = " ") -> str:
    return sep.join(f"{b:02X}" for b in data)


def parse_int(text, *, bits: int | None = None) -> int:
    """Parse '0x1A', '1Ah', '26', 26 → int with optional range check."""
    if isinstance(text, int):
        value = text
    else:
        s = str(text).strip().lower().replace("_", "")
        if not s:
            raise ValueError("пустое число")
        neg = s.startswith("-")
        if neg:
            s = s[1:]
        if s.startswith("0x"):
            body, base = s[2:], 16
        elif s.endswith("h"):
            body, base = s[:-1], 16
        elif s.startswith("0b"):
            body, base = s[2:], 2
        else:
            body, base = s, 10
        allowed = {16: _HEX, 10: set(string.digits), 2: {"0", "1"}}[base]
        if not body or any(c not in allowed for c in body):
            raise ValueError(f"не число: {text!r}")
        value = int(body, base)
        if neg:
            value = -value
    if bits is not None and not (0 <= value < (1 << bits)):
        raise ValueError(f"значение {value} вне диапазона 0..{(1 << bits) - 1}")
    return value
