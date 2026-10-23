"""Long coding (DID 0x0600) model: byte/bit access, diffs and pretty-printing.

Byte and bit numbering follows VCDS/OBDeleven: Byte 0 is the first byte,
Bit 0 is the least significant bit (value 0x01), Bit 7 is 0x80.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..hexutil import to_hex


@dataclass(frozen=True)
class BitChange:
    byte: int
    bit: int
    old: int
    new: int

    def __str__(self) -> str:
        return f"Byte {self.byte} Bit {self.bit}: {self.old} → {self.new}"


class LongCoding:
    def __init__(self, data: bytes):
        self.data = bytearray(data)

    @classmethod
    def from_hex(cls, text: str) -> "LongCoding":
        from ..hexutil import parse_hex
        return cls(parse_hex(text))

    def __len__(self) -> int:
        return len(self.data)

    def __bytes__(self) -> bytes:
        return bytes(self.data)

    def copy(self) -> "LongCoding":
        return LongCoding(bytes(self.data))

    def _check(self, byte: int, bit: int = 0) -> None:
        if not (0 <= byte < len(self.data)):
            raise IndexError(f"Byte {byte} вне кодировки длиной {len(self.data)} байт (0..{len(self.data) - 1})")
        if not (0 <= bit <= 7):
            raise IndexError(f"Bit {bit} вне диапазона 0..7")

    def get_bit(self, byte: int, bit: int) -> int:
        self._check(byte, bit)
        return (self.data[byte] >> bit) & 1

    def set_bit(self, byte: int, bit: int, value: int | bool) -> None:
        self._check(byte, bit)
        if value:
            self.data[byte] |= (1 << bit)
        else:
            self.data[byte] &= ~(1 << bit) & 0xFF

    def get_field(self, byte: int, mask: int) -> int:
        self._check(byte)
        return self.data[byte] & mask

    def set_field(self, byte: int, mask: int, value: int) -> None:
        self._check(byte)
        if value & ~mask & 0xFF:
            raise ValueError(f"значение 0x{value:02X} не помещается в маску 0x{mask:02X}")
        self.data[byte] = (self.data[byte] & ~mask & 0xFF) | (value & mask)

    def hex(self, sep: str = "") -> str:
        return to_hex(bytes(self.data), sep)

    def diff(self, other: "LongCoding") -> list[BitChange]:
        if len(other) != len(self):
            raise ValueError("кодировки разной длины")
        out = []
        for i, (a, b) in enumerate(zip(self.data, other.data)):
            x = a ^ b
            for bit in range(8):
                if x >> bit & 1:
                    out.append(BitChange(i, bit, (a >> bit) & 1, (b >> bit) & 1))
        return out

    def table(self) -> str:
        """Human-readable byte table with binary view (Bit 7 … Bit 0)."""
        lines = ["Byte  Hex  Bits(7..0)"]
        for i, b in enumerate(self.data):
            lines.append(f"{i:>4}  {b:02X}   {b:08b}")
        return "\n".join(lines)
