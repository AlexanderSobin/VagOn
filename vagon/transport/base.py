"""Transport abstractions.

A *Link* is a raw byte pipe to an adapter (TCP socket, serial port, BLE).
A *Transport* speaks to the car through an adapter and hands out *Channels*,
each bound to one diagnostic target (request CAN ID → response CAN ID).
A Channel moves whole UDS payloads; ISO-TP framing is the transport's job.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Callable

TraceHook = Callable[[str, str], None]  # (direction, text)


class Link(ABC):
    """Byte stream to an adapter."""

    name: str = "link"

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    @abstractmethod
    def read(self, timeout: float) -> bytes:
        """Return whatever arrived within `timeout` seconds (may be b'')."""

    @abstractmethod
    def close(self) -> None: ...

    def drain(self, quiet: float = 0.05, limit: float = 1.0) -> bytes:
        """Read and return everything until the line is quiet for `quiet` seconds."""
        out = bytearray()
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            chunk = self.read(quiet)
            if not chunk:
                break
            out += chunk
        return bytes(out)


class Channel(ABC):
    """A request/response pipe to a single module."""

    tx_id: int
    rx_id: int

    @abstractmethod
    def exchange(self, payload: bytes, timeout: float) -> list[bytes]:
        """Send one UDS request and return all complete UDS responses from the module
        received until the adapter/bus went quiet (may include 0x7F xx 0x78 'pending')."""

    def listen(self, timeout: float) -> list[bytes]:
        """Wait for more responses without sending (after 'response pending').
        Transports that cannot listen return []."""
        return []


class Transport(ABC):
    description: str = "transport"

    @abstractmethod
    def channel(self, tx_id: int, rx_id: int) -> Channel: ...

    @abstractmethod
    def close(self) -> None: ...

    def info(self) -> dict:
        return {"description": self.description}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
