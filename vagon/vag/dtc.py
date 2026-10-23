"""UDS fault code (DTC) formatting."""

from __future__ import annotations

_LETTERS = "PCBU"

STATUS_BITS = [
    (0x01, "сейчас активна", "testFailed"),
    (0x02, "в этом цикле", "testFailedThisOperationCycle"),
    (0x04, "ожидающая", "pending"),
    (0x08, "подтверждённая", "confirmed"),
    (0x10, "тест не завершён после сброса", "testNotCompletedSinceLastClear"),
    (0x20, "была после сброса", "testFailedSinceLastClear"),
    (0x40, "тест не завершён в этом цикле", "testNotCompletedThisOperationCycle"),
    (0x80, "горит лампа", "warningIndicatorRequested"),
]


def format_dtc(code: int) -> str:
    """0x9A0B00 → 'B1A0B 00' (SAE letter + 4 digits + failure type byte)."""
    b0, b1, ftb = (code >> 16) & 0xFF, (code >> 8) & 0xFF, code & 0xFF
    letter = _LETTERS[b0 >> 6]
    return f"{letter}{(b0 >> 4) & 0x3:X}{b0 & 0x0F:X}{b1:02X} {ftb:02X}"


def describe_status(status: int, lang: str = "ru") -> str:
    idx = 1 if lang == "ru" else 2
    names = [entry[idx] for entry in STATUS_BITS if status & entry[0]]
    return ", ".join(names) or ("нет флагов" if lang == "ru" else "no flags")


def is_active(status: int) -> bool:
    return bool(status & 0x01)
