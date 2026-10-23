import json
import threading
import urllib.error
import urllib.request

import pytest

from vagon.app import App
from vagon.cli import main
from vagon.transport.ble import PythonistaLink, choose_uart, looks_like_obd, short_uuid


# ------------------------------------------------------------------ web API
@pytest.fixture
def web():
    from vagon.web.server import make_server
    app = App("sim://?fresh=1", trace=False)
    srv = make_server(app, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base
    srv.shutdown()
    srv.server_close()
    app.disconnect()


def call(base, path, body=None, header=True):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method="POST" if body is not None else "GET")
    if body is not None:
        req.add_header("Content-Type", "application/json")
        if header:
            req.add_header("X-VagOn", "1")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_web_flow(web):
    code, page = 200, urllib.request.urlopen(web + "/").read()
    assert b"VagOn" in page
    assert call(web, "/api/connect", {"adapter": "sim://?fresh=1"}, header=False)[0] == 403
    code, st = call(web, "/api/connect", {"adapter": "sim://?fresh=1"})
    assert code == 200 and st["connected"] and st["vin"] == "WAUZZZGY0MA000000"
    code, sc = call(web, "/api/scan", {"dtc": True})
    assert code == 200 and any(m["address"] == "5F" for m in sc["modules"])
    code, m = call(web, "/api/module/17")
    assert code == 200 and len(m["coding"]["bytes"]) == 20
    code, dry = call(web, "/api/module/17/coding", {"bits": [{"byte": 1, "bit": 0, "value": 1}]})
    assert dry["dry_run"] is True
    code, rec = call(web, "/api/recipes")
    assert any(r["id"] == "mmi.wireless_carplay" for r in rec["recipes"])
    code, plan = call(web, "/api/recipes/mmi.wireless_carplay/plan", {"action": "on"})
    assert code == 200 and not plan["blockers"]
    assert call(web, "/api/recipes/mmi.wireless_carplay/apply", {"action": "on"})[0] == 400  # no confirm
    code, res = call(web, "/api/recipes/mmi.wireless_carplay/apply", {"action": "on", "confirm": True})
    assert code == 200 and res["results"][0]["verified"]
    code, ch = call(web, "/api/module/19/channel", {"name": "Start_stop_voltage_limit", "value": "12.1",
                                                    "dry_run": False})
    assert code == 200 and ch["new"] == "79"
    code, bk = call(web, "/api/backups")
    assert len(bk["backups"]) >= 2
    code, dt = call(web, "/api/dtc", {"modules": ["09"]})
    assert dt["modules"][0]["dtcs"][0]["code"] == "B1A0B 00"
    code, ls = call(web, "/api/learn/start", {"module": "09"})
    assert code == 200 and ls["count"] >= 4
    assert call(web, "/api/sim/change", {"module": "09", "did": "0x0903", "hex": "06"})[0] == 200
    code, lf = call(web, "/api/learn/finish", {"module": "09", "name": "Leaving home", "old": "10", "new": "60",
                                               "recipe_name": "Ухожу из дома"})
    assert code == 200 and lf["channel"]["did"] == "0x0903" and lf["recipe"] == "user.uhozhu_iz_doma"
    code, j = call(web, "/api/journal")
    assert j["entries"][0]["status"] == "ok"
    assert call(web, "/api/nope", {})[0] == 404


def test_web_requires_connection(web):
    code, err = call(web, "/api/scan", {})
    assert code == 409 and "не подключён" in err["error"]


# ------------------------------------------------------------------ BLE helpers
def test_uart_choice():
    assert short_uuid("0000fff1-0000-1000-8000-00805f9b34fb") == "FFF1"
    assert choose_uart({"FFF0": [("FFF1", {"notify"}), ("FFF2", {"write"})]}) == ("FFF1", "FFF2")
    assert choose_uart({"FFE0": [("FFE1", {"notify", "write-without-response"})]}) == ("FFE1", "FFE1")
    assert choose_uart({"1234": [("AAAA", {"indicate"}), ("BBBB", {"write"})]}) == ("AAAA", "BBBB")
    assert looks_like_obd("OBDII") and looks_like_obd("IOS-Vlink") and not looks_like_obd("AirPods")


class FakeChar:
    def __init__(self, uuid, props):
        self.uuid, self.properties, self.value = uuid, props, None


class FakeService:
    def __init__(self, uuid, chars):
        self.uuid, self.characteristics = uuid, chars


class FakePeripheral:
    def __init__(self, cb, name):
        self.cb, self.name, self.uuid = cb, name, "UUID-1"
        self.rx = FakeChar("FFF1", 0x10)
        self.tx = FakeChar("FFF2", 0x08)
        self.services = [FakeService("FFF0", [self.rx, self.tx])]
        self.notify = False
        self.written = []

    def discover_services(self):
        threading.Thread(target=self.cb.delegate.did_discover_services, args=(self, None)).start()

    def discover_characteristics(self, s):
        threading.Thread(target=self.cb.delegate.did_discover_characteristics, args=(s, None)).start()

    def set_notify_value(self, c, flag):
        self.notify = flag

    def write_characteristic_value(self, c, data, with_response):
        self.written.append((bytes(data), with_response))
        if data.endswith(b"\r"):
            self.rx.value = b"OK\r\r>"
            self.cb.delegate.did_update_value(self.rx, None)


class FakeCB:
    CH_PROP_NOTIFY, CH_PROP_WRITE, CH_PROP_WRITE_WITHOUT_RESPONSE, CH_PROP_INDICATE = 0x10, 0x08, 0x04, 0x20

    def __init__(self):
        self.delegate = None
        self.peripherals = [FakePeripheral(self, "AirPods"), FakePeripheral(self, "OBDII")]
        self.connected = None

    def set_central_delegate(self, d):
        self.delegate = d

    def scan_for_peripherals(self):
        def go():
            for p in self.peripherals:
                self.delegate.did_discover_peripheral(p)
        threading.Thread(target=go).start()

    def stop_scan(self):
        pass

    def connect_peripheral(self, p):
        self.connected = p
        threading.Thread(target=self.delegate.did_connect_peripheral, args=(p,)).start()

    def cancel_peripheral_connection(self, p):
        self.connected = None

    def reset(self):
        pass


def test_pythonista_link_discovery_and_io():
    cb = FakeCB()
    link = PythonistaLink(cb_module=cb, scan_timeout=2)
    assert link.name == "BLE OBDII" and cb.connected.name == "OBDII"
    assert cb.connected.notify
    link.write(b"ATZ_LONG_COMMAND_OVER_20_BYTES\r")
    assert all(len(d) <= 20 for d, _ in cb.connected.written)
    assert b"".join(d for d, _ in cb.connected.written) == b"ATZ_LONG_COMMAND_OVER_20_BYTES\r"
    assert link.read(1.0) == b"OK\r\r>"
    link.close()


# ------------------------------------------------------------------ CLI
def test_cli_commands(capsys):
    assert main(["-a", "sim://?fresh=1", "--no-trace", "scan", "-q"]) == 0
    assert "Найдено блоков" in capsys.readouterr().out
    assert main(["-a", "sim", "--no-trace", "recipe", "on", "cockpit.needle_sweep", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "проверено чтением" in out or "Ничего менять не нужно" in out
    assert main(["-a", "sim", "--no-trace", "recipe", "check", "cockpit.needle_sweep"]) == 0
    assert "включено" in capsys.readouterr().out
    assert main(["-a", "sim", "--no-trace", "coding", "09", "set", "0", "0", "on"]) in (0, 1)
    assert main(["-a", "sim", "--no-trace", "ident", "ZZ"]) == 2
    assert "неверный адрес" in capsys.readouterr().out


def test_cli_no_yes_means_no_write(capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    main(["-a", "sim", "--no-trace", "recipe", "off", "cockpit.needle_sweep"])
    main(["-a", "sim", "--no-trace", "recipe", "on", "cockpit.needle_sweep"])
    assert "Нет подтверждения" in capsys.readouterr().out


def test_web_raw_did_and_backup_all(web):
    call(web, "/api/connect", {"adapter": "sim://?fresh=1"})
    code, r = call(web, "/api/module/09/did_read", {"did": "0x0902"})
    assert code == 200 and r["hex"] == "01"
    code, dry = call(web, "/api/module/09/did_write", {"did": "0x0902", "hex": "03"})
    assert dry["dry_run"] and dry["old"] == "01"
    code, res = call(web, "/api/module/09/did_write", {"did": "0x0902", "hex": "03", "confirm": True})
    assert code == 200 and res["result"]["verified"]
    code, b = call(web, "/api/backup_all", {})
    assert code == 200 and len(b["saved"]) >= 10


def test_cli_dtc_only_found_modules(capsys, monkeypatch):
    from vagon.vag import vehicle as veh
    calls = []
    orig = veh.ModuleSession.read_dtcs

    def spy(self):
        calls.append(self.module.addr_hex)
        return orig(self)
    monkeypatch.setattr(veh.ModuleSession, "read_dtcs", spy)
    assert main(["-a", "sim://?fresh=1", "--no-trace", "dtc"]) == 0
    assert "B1A0B 00" in capsys.readouterr().out
    assert "82" not in calls and len(calls) == 15        # HUD isn't in this car
