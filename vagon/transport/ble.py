"""Bluetooth Low Energy links for BLE ELM327 adapters.

iPhone can only talk to BLE (or Wi-Fi) adapters — classic Bluetooth SPP is
not available to apps on iOS. Two backends:

* BleakLink      — macOS/Linux/Windows desktop, needs `pip3 install bleak`
* PythonistaLink — iPhone/iPad inside Pythonista 3 (uses its `cb` module)

Both pick the UART-like characteristics automatically.
"""

from __future__ import annotations

import threading
import time

from ..errors import TransportError
from .base import Link

# (service, notify characteristic, write characteristic) seen on ELM327 BLE clones
KNOWN_UART = [
    ("FFF0", "FFF1", "FFF2"),
    ("FFE0", "FFE1", "FFE1"),
    ("18F0", "2AF0", "2AF1"),
    ("E7810A71-73AE-499D-8C15-FAA9AEF0C3F2", "BEF8D6C9-9C21-4C9E-B632-BD58C1009F9F",
     "BEF8D6C9-9C21-4C9E-B632-BD58C1009F9F"),
]

NAME_HINTS = ("obd", "elm", "vlink", "v-link", "vlinker", "icar", "rokodil", "scanx", "konnwei",
              "veepeak", "carista", "obdlink", "ios-", "ble", "link", "scan", "car")

CHUNK = 20  # safe ATT payload for the default MTU


def short_uuid(u: str) -> str:
    """'0000fff1-0000-1000-8000-00805f9b34fb' → 'FFF1'; others upper-cased."""
    u = str(u).upper()
    if len(u) == 36 and u.endswith("-0000-1000-8000-00805F9B34FB") and u.startswith("0000"):
        return u[4:8]
    return u


def looks_like_obd(name: str | None) -> bool:
    n = (name or "").lower()
    return any(h in n for h in NAME_HINTS)


def choose_uart(services: dict[str, list[tuple[str, set[str]]]]) -> tuple[str, str]:
    """services: {service_uuid: [(char_uuid, {'notify','write','write-without-response',...})]}.
    Return (notify_char_uuid, write_char_uuid)."""
    norm = {short_uuid(s): [(short_uuid(c), props) for c, props in chars]
            for s, chars in services.items()}
    for svc, rx, tx in KNOWN_UART:
        chars = dict(norm.get(svc, []))
        if rx in chars and tx in chars:
            return rx, tx
    for svc, chars in norm.items():
        rx = next((c for c, p in chars if {"notify", "indicate"} & p), None)
        tx = next((c for c, p in chars if {"write", "write-without-response"} & p), None)
        if rx and tx:
            return rx, tx
    raise TransportError("у BLE-устройства не найден UART-сервис ELM327")


class _RxBuffer:
    def __init__(self) -> None:
        self._buf = bytearray()
        self._cond = threading.Condition()

    def push(self, data: bytes) -> None:
        with self._cond:
            self._buf += data
            self._cond.notify_all()

    def pop(self, timeout: float) -> bytes:
        with self._cond:
            if not self._buf and timeout > 0:
                self._cond.wait(timeout)
            data = bytes(self._buf)
            self._buf.clear()
            return data


# --------------------------------------------------------------------------- bleak
class BleakLink(Link):
    def __init__(self, target: str | None = None, scan_timeout: float = 8.0):
        import asyncio
        import importlib.util
        if importlib.util.find_spec("bleak") is None:
            raise TransportError("для BLE на Mac нужен bleak: pip3 install bleak")
        self._asyncio = asyncio
        self._rx = _RxBuffer()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="vagon-ble")
        self._thread.start()
        self._client = None
        try:
            self._run(self._connect(target, scan_timeout), timeout=scan_timeout + 25)
        except Exception:
            self.close()
            raise

    def _run(self, coro, timeout: float):
        fut = self._asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout)
        except TransportError:
            raise
        except Exception as e:
            raise TransportError(f"BLE: {e}") from e

    async def _connect(self, target, scan_timeout):
        from bleak import BleakClient, BleakScanner  # type: ignore
        device = None
        if target and (":" in target or len(target) == 36):
            device = await BleakScanner.find_device_by_address(target, timeout=scan_timeout)
        else:
            found = await BleakScanner.discover(timeout=scan_timeout)
            want = (target or "").lower()
            for d in found:
                if (want and want in (d.name or "").lower()) or (not want and looks_like_obd(d.name)):
                    device = d
                    break
        if device is None:
            raise TransportError("BLE-адаптер не найден. Вставьте его в OBD, включите зажигание, "
                                 "и укажите имя: --adapter ble://ИМЯ (см. vagon ble-scan)")
        self.name = f"BLE {device.name or device.address}"
        client = BleakClient(device)
        await client.connect()
        self._client = client
        services = {}
        for svc in client.services:
            services[svc.uuid] = [(c.uuid, set(c.properties)) for c in svc.characteristics]
        rx, tx = choose_uart(services)
        self._rx_uuid = self._full(rx)
        self._tx_uuid = self._full(tx)
        tx_props = next(p for s in services.values() for c, p in s if short_uuid(c) == tx)
        self._with_response = "write-without-response" not in tx_props
        await client.start_notify(self._rx_uuid, lambda _h, data: self._rx.push(bytes(data)))

    @staticmethod
    def _full(u: str) -> str:
        return f"0000{u}-0000-1000-8000-00805f9b34fb".lower() if len(u) == 4 else u.lower()

    def write(self, data: bytes) -> None:
        if not self._client:
            raise TransportError("BLE не подключён")
        for i in range(0, len(data), CHUNK):
            self._run(self._client.write_gatt_char(self._tx_uuid, data[i:i + CHUNK],
                                                   response=self._with_response), timeout=5)

    def read(self, timeout: float) -> bytes:
        return self._rx.pop(timeout)

    def close(self) -> None:
        try:
            if self._client:
                self._run(self._client.disconnect(), timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)


def bleak_scan(timeout: float = 8.0) -> list[tuple[str, str]]:
    import asyncio
    try:
        from bleak import BleakScanner  # type: ignore
    except ImportError as e:
        raise TransportError("для BLE на Mac нужен bleak: pip3 install bleak") from e

    async def go():
        return await BleakScanner.discover(timeout=timeout)
    return [(d.name or "?", d.address) for d in asyncio.run(go())]


# ----------------------------------------------------------------------- Pythonista
class PythonistaLink(Link):
    """BLE via Pythonista's `cb` (CoreBluetooth) module on iOS."""

    def __init__(self, target: str | None = None, scan_timeout: float = 10.0, cb_module=None):
        if cb_module is None:
            try:
                import cb  # type: ignore
            except ImportError as e:
                raise TransportError("модуль cb доступен только в Pythonista на iPhone/iPad") from e
            cb_module = cb
        self.cb = cb_module
        self._rx = _RxBuffer()
        self._want = (target or "").lower()
        self._peripheral = None
        self._rx_char = None
        self._tx_char = None
        self._ready = threading.Event()
        self._error: str | None = None
        self._pending_services = 0
        self._chars: dict = {}
        self.name = "BLE"
        self.cb.set_central_delegate(self)
        self.cb.scan_for_peripherals()
        if not self._ready.wait(scan_timeout + 15):
            self.cb.stop_scan()
            self.cb.reset()
            raise TransportError(self._error or "BLE-адаптер не найден или не ответил (Pythonista)")
        if self._error:
            self.cb.reset()
            raise TransportError(self._error)

    # --- cb delegate callbacks (called on a background thread by Pythonista)
    def did_discover_peripheral(self, p):
        name = p.name or ""
        if self._peripheral is not None:
            return
        if (self._want and (self._want in name.lower() or self._want == str(p.uuid).lower())) or \
                (not self._want and looks_like_obd(name)):
            self._peripheral = p
            self.name = f"BLE {name}"
            self.cb.stop_scan()
            self.cb.connect_peripheral(p)

    def did_connect_peripheral(self, p):
        p.discover_services()

    def did_fail_to_connect_peripheral(self, p, error):
        self._error = f"BLE: не удалось подключиться ({error})"
        self._ready.set()

    def did_disconnect_peripheral(self, p, error):
        if not self._ready.is_set():
            self._error = f"BLE: отключение ({error})"
            self._ready.set()

    def did_discover_services(self, p, error):
        if error:
            self._error = f"BLE: ошибка сервисов ({error})"
            self._ready.set()
            return
        self._pending_services = len(p.services)
        for s in p.services:
            p.discover_characteristics(s)

    def did_discover_characteristics(self, s, error):
        props = []
        for c in s.characteristics:
            pset = set()
            if c.properties & getattr(self.cb, "CH_PROP_NOTIFY", 0x10):
                pset.add("notify")
            if c.properties & getattr(self.cb, "CH_PROP_INDICATE", 0x20):
                pset.add("indicate")
            if c.properties & getattr(self.cb, "CH_PROP_WRITE", 0x08):
                pset.add("write")
            if c.properties & getattr(self.cb, "CH_PROP_WRITE_WITHOUT_RESPONSE", 0x04):
                pset.add("write-without-response")
            props.append((c.uuid, pset))
            self._chars[short_uuid(c.uuid)] = (c, pset)
        self._services = getattr(self, "_services", {})
        self._services[s.uuid] = props
        self._pending_services -= 1
        if self._pending_services <= 0:
            try:
                rx, tx = choose_uart(self._services)
            except TransportError as e:
                self._error = str(e)
                self._ready.set()
                return
            self._rx_char = self._chars[rx][0]
            self._tx_char, tx_props = self._chars[tx]
            self._with_response = "write-without-response" not in tx_props
            self._peripheral.set_notify_value(self._rx_char, True)
            self._ready.set()

    def did_update_value(self, c, error):
        if c.value:
            self._rx.push(bytes(c.value))

    def did_write_value(self, c, error):
        pass

    # --- Link API
    def write(self, data: bytes) -> None:
        if self._peripheral is None or self._tx_char is None:
            raise TransportError("BLE не подключён")
        for i in range(0, len(data), CHUNK):
            self._peripheral.write_characteristic_value(self._tx_char, data[i:i + CHUNK],
                                                        self._with_response)
            time.sleep(0.005)

    def read(self, timeout: float) -> bytes:
        return self._rx.pop(timeout)

    def close(self) -> None:
        try:
            if self._peripheral is not None:
                self.cb.cancel_peripheral_connection(self._peripheral)
        finally:
            self.cb.reset()
