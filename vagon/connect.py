"""Open an adapter from a short URL — the same syntax on Mac and iPhone.

  sim                         built-in simulated A3 8Y (no car needed)
  sim://?sfd=5F,17&stn=1      simulator with SFD-locked 5F and 17, STN adapter
  wifi                        Wi-Fi ELM327 at 192.168.0.10:35000
  tcp://192.168.4.1:35000     Wi-Fi ELM327 at a custom address
  ble                         first BLE adapter that looks like an OBD dongle
  ble://OBDII                 BLE adapter by name (or macOS UUID)
  serial:///dev/cu.OBDII      serial / Bluetooth-classic port (Mac), ?baud=38400
  /dev/cu.usbserial-1410      same, short form
"""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlparse

from .errors import TransportError
from .paths import is_pythonista
from .transport.base import TraceHook
from .transport.elm327 import Elm327

DEFAULT_WIFI = ("192.168.0.10", 35000)


def default_adapter() -> str:
    env = os.environ.get("VAGON_ADAPTER")
    if env:
        return env
    return "ble" if is_pythonista() else "wifi"


def is_sim(url: str | None) -> bool:
    return (url or default_adapter()).startswith("sim")


def open_adapter(url: str | None = None, *, trace: TraceHook | None = None, st_min_ms: int = 10,
                 sim_state=None) -> Elm327:
    url = (url or default_adapter()).strip()
    if url == "sim" or url.startswith("sim:"):
        from .sim.car import make_sim_elm
        from .sim.link import SimLink
        q = parse_qs(urlparse(url).query) if "?" in url else {}
        sfd = [s.strip().upper() for s in ",".join(q.get("sfd", [])).split(",") if s.strip()]
        if sim_state is None:
            sim_state = make_sim_elm(stn=q.get("stn", ["0"])[0] == "1", sfd_5f="5F" in sfd,
                                     sfd_17="17" in sfd)
        link = SimLink(sim_state)
        return Elm327(link, trace=trace, st_min_ms=st_min_ms).connect()

    if url == "wifi":
        url = f"tcp://{DEFAULT_WIFI[0]}:{DEFAULT_WIFI[1]}"
    if url.startswith("tcp://"):
        from .transport.links import TcpLink
        p = urlparse(url)
        link = TcpLink(p.hostname or DEFAULT_WIFI[0], p.port or DEFAULT_WIFI[1])
        try:
            return Elm327(link, trace=trace, st_min_ms=st_min_ms).connect()
        except Exception:
            link.close()
            raise

    if url == "ble" or url.startswith("ble:"):
        target = url[len("ble://"):] if url.startswith("ble://") else None
        if is_pythonista():
            from .transport.ble import PythonistaLink
            link = PythonistaLink(target or None)
        else:
            from .transport.ble import BleakLink
            link = BleakLink(target or None)
        try:
            return Elm327(link, trace=trace, st_min_ms=st_min_ms).connect()
        except Exception:
            link.close()
            raise

    if url.startswith("serial://") or url.startswith("/dev/") or url.upper().startswith("COM"):
        from .transport.links import SerialLink
        if url.startswith("serial://"):
            p = urlparse(url)
            port = p.path or p.netloc
            q = parse_qs(p.query)
            bauds = [int(q["baud"][0])] if "baud" in q else [38400, 115200, 9600, 500000]
        else:
            port, bauds = url, [38400, 115200, 9600, 500000]
        last: Exception | None = None
        for baud in bauds:
            link = SerialLink(port, baud)
            try:
                return Elm327(link, trace=trace, st_min_ms=st_min_ms).connect()
            except TransportError as e:
                last = e
                link.close()
        raise TransportError(f"адаптер на {port} не ответил ни на одной скорости ({last})")

    raise TransportError(f"не понимаю адрес адаптера {url!r}. Примеры: wifi, ble, sim, "
                         "tcp://192.168.0.10:35000, serial:///dev/cu.OBDII")
