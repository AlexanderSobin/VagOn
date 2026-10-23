"""Byte links to adapters: Wi-Fi (TCP), serial (USB / Bluetooth-classic on macOS)."""

from __future__ import annotations

import select
import socket
import time

from ..errors import TransportError
from .base import Link


class TcpLink(Link):
    """Wi-Fi ELM327 adapters: usually 192.168.0.10:35000 (some 192.168.4.1:35000)."""

    def __init__(self, host: str = "192.168.0.10", port: int = 35000, connect_timeout: float = 6.0):
        self.name = f"Wi-Fi {host}:{port}"
        try:
            self.sock = socket.create_connection((host, port), timeout=connect_timeout)
        except OSError as e:
            raise TransportError(
                f"не удалось подключиться к {host}:{port} ({e}). Подключитесь к Wi-Fi сети адаптера "
                "(обычно WiFi_OBDII / OBDII / V-LINK) и проверьте IP/порт.") from e
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.setblocking(False)

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        deadline = time.monotonic() + 5.0
        while view:
            if time.monotonic() > deadline:
                raise TransportError("Wi-Fi: таймаут отправки")
            try:
                n = self.sock.send(view)
                view = view[n:]
            except BlockingIOError:
                select.select([], [self.sock], [], 0.1)
            except OSError as e:
                raise TransportError(f"Wi-Fi: связь потеряна ({e})") from e

    def read(self, timeout: float) -> bytes:
        try:
            r, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
            if not r:
                return b""
            data = self.sock.recv(4096)
        except BlockingIOError:
            return b""
        except OSError as e:
            raise TransportError(f"Wi-Fi: связь потеряна ({e})") from e
        if data == b"":
            raise TransportError("Wi-Fi: адаптер закрыл соединение")
        return data

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class SerialLink(Link):
    """USB-serial or Bluetooth-classic (macOS /dev/cu.*) adapters. Needs pyserial."""

    def __init__(self, port: str, baud: int = 38400):
        try:
            import serial  # type: ignore
        except ImportError as e:
            raise TransportError("для последовательного порта нужен pyserial: pip3 install pyserial") from e
        self.name = f"serial {port}@{baud}"
        try:
            self.ser = serial.Serial(port, baudrate=baud, timeout=0, write_timeout=5)
        except Exception as e:  # serial.SerialException
            raise TransportError(f"не удалось открыть порт {port}: {e}") from e

    def write(self, data: bytes) -> None:
        try:
            self.ser.write(data)
            self.ser.flush()
        except Exception as e:
            raise TransportError(f"порт: ошибка записи ({e})") from e

    def read(self, timeout: float) -> bytes:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                n = self.ser.in_waiting
                if n:
                    return self.ser.read(n)
            except Exception as e:
                raise TransportError(f"порт: ошибка чтения ({e})") from e
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.005)

    def close(self) -> None:
        try:
            self.ser.close()
        except Exception:
            pass


def list_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports  # type: ignore
    except ImportError:
        import glob
        return sorted(glob.glob("/dev/cu.*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/rfcomm*"))
    return [p.device for p in list_ports.comports()]
