"""Links that connect the VagOn driver to the emulator (in-process or over TCP)."""

from __future__ import annotations

import socket
import socketserver
import threading

from ..transport.base import Link
from .elm import SimElm327


class SimLink(Link):
    """In-process link: bytes go straight into a SimElm327."""

    def __init__(self, elm: SimElm327):
        self.elm = elm
        self.name = "симулятор"
        self.closed = False
        self._lock = threading.Lock()

    def write(self, data: bytes) -> None:
        with self._lock:
            self.elm.write(data)

    def read(self, timeout: float) -> bytes:
        with self._lock:
            return self.elm.read()

    def close(self) -> None:
        self.closed = True


def serve_tcp(car_factory, host: str = "127.0.0.1", port: int = 35000,
              ready: threading.Event | None = None, stop: threading.Event | None = None):
    """Run a Wi-Fi-ELM327-like TCP server backed by the emulator.

    `car_factory()` returns a SimElm327; the same instance is reused across
    connections so state (coding changes) survives reconnects, like a real car.
    """
    elm_holder = {"elm": None}
    lock = threading.Lock()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            with lock:
                if elm_holder["elm"] is None:
                    elm_holder["elm"] = car_factory()
                elm = elm_holder["elm"]
            sock: socket.socket = self.request
            sock.settimeout(0.2)
            while not (stop and stop.is_set()):
                try:
                    data = sock.recv(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                with lock:
                    elm.write(data)
                    out = elm.read()
                if out:
                    try:
                        sock.sendall(out)
                    except OSError:
                        break

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    with Server((host, port), Handler) as srv:
        srv.timeout = 0.2
        if ready:
            ready.set()
        while not (stop and stop.is_set()):
            srv.handle_request()
