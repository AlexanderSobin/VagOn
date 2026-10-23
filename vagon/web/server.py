"""Local web UI: a JSON API over the App + a single offline HTML page.

Runs on the Mac (open http://127.0.0.1:8765 in a browser) and inside
Pythonista on iPhone (shown in Pythonista's built-in browser, so it keeps
running in the foreground). Binds to localhost only. Every POST must carry the
header 'X-VagOn: 1' — browsers can't add it cross-origin without a CORS
preflight (which this server never approves), so other web pages cannot drive
writes to the car.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import __version__
from ..app import App
from ..errors import NegativeResponse, TimeoutError_, VagOnError
from ..hexutil import parse_hex, parse_int
from ..recipes import apply_plan, build_learned_recipe, plan_recipe, save_user_recipe
from ..vag.dtc import describe_status, format_dtc
from ..vag.modules import MODULES, get_module
from ..vag.vehicle import did_key

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


class Progress:
    def __init__(self) -> None:
        self.message = ""
        self.busy = False
        self.started = 0.0
        self._lock = threading.Lock()

    def __call__(self, msg: str) -> None:
        with self._lock:
            self.message = msg

    def begin(self, msg: str) -> None:
        with self._lock:
            self.busy, self.message, self.started = True, msg, time.monotonic()

    def end(self) -> None:
        with self._lock:
            self.busy, self.message = False, ""

    def snapshot(self) -> dict:
        with self._lock:
            return {"busy": self.busy, "message": self.message,
                    "seconds": round(time.monotonic() - self.started, 1) if self.busy else 0}


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _ident_dict(i) -> dict:
    return i.to_dict() if i else {}


class Api:
    def __init__(self, app: App):
        self.app = app
        self.progress = Progress()
        self.op_lock = threading.Lock()

    # ------------------------------------------------------------------ helpers
    def _vehicle(self):
        if self.app.vehicle is None:
            raise ApiError("адаптер не подключён", 409)
        return self.app.vehicle

    def run(self, label: str, fn):
        if not self.op_lock.acquire(blocking=False):
            raise ApiError("уже выполняется другая операция: " + (self.progress.message or "…"), 409)
        self.progress.begin(label)
        try:
            with self.app.lock:
                return fn()
        finally:
            self.progress.end()
            self.op_lock.release()

    # ------------------------------------------------------------------ endpoints
    def status(self, _q) -> dict:
        v = self.app.vehicle
        info = v.transport.info() if v else None
        return {"version": __version__, "connected": v is not None, "adapter": self.app.adapter_url,
                "sim": self.app.sim, "info": info, "vin": v.vin if v else None,
                "progress": self.progress.snapshot(),
                "log": self.app.trace.path if self.app.trace else None,
                "modules_found": [{"address": e.module.addr_hex, "name": e.module.name_ru,
                                   "ident": _ident_dict(e.ident)} for e in (v.found if v else [])]}

    def connect(self, body) -> dict:
        adapter = (body.get("adapter") or "").strip() or None

        def go():
            from ..vag.check import adapter_check, format_check
            # an explicit "connect" always starts fresh (a dropped BLE link leaves a dead transport);
            # done under the operation lock so it can never cut into a running write
            self.app.disconnect()
            self.app.connect(adapter)
            v = self.app.vehicle
            try:
                v.read_vin()
            except VagOnError:
                pass
            st = self.status({})
            try:
                chk = adapter_check(v)
                st["check"] = {"ok": chk["ok"], "lines": format_check(chk)}
            except VagOnError as e:
                st["check"] = {"ok": False, "lines": [str(e)]}
            return st
        return self.run("подключение к адаптеру…", go)

    def disconnect(self, _body) -> dict:
        # refused while another operation (e.g. a write) is running
        return self.run("отключение…", lambda: (self.app.disconnect(), self.status({}))[1])

    def scan(self, body) -> dict:
        v = self._vehicle()

        def go():
            entries = v.scan(progress=self.progress, with_dtc=bool(body.get("dtc")))
            return {"vin": v.vin, "modules": [
                {"address": e.module.addr_hex, "name": e.module.name_ru, "protected": e.module.protected,
                 "ident": _ident_dict(e.ident), "dtc_count": e.dtc_count}
                for e in entries if e.found]}
        return self.run("опрос блоков…", go)

    def module(self, addr: str, _q) -> dict:
        v = self._vehicle()

        def go():
            s = v.module(addr)
            ident = s.ident(refresh=True)
            coding = None
            if ident.coding:
                raw = bytes.fromhex(ident.coding)
                coding = {"hex": ident.coding, "bytes": list(raw)}
            channels = []
            for ch in self.app.labels.channels(s.module.addr_hex, ident.odx_dataset):
                try:
                    raw = ch.extract(s.read_did(ch.did))
                    channels.append({"name": ch.name, "value": ch.value_name(raw), "raw": raw,
                                     "options": list(ch.values), "numeric": ch.scale is not None,
                                     "unit": ch.unit, "did": did_key(ch.did)})
                except (NegativeResponse, TimeoutError_, ValueError) as e:
                    channels.append({"name": ch.name, "error": str(e), "did": did_key(ch.did)})
            return {"address": s.module.addr_hex, "name": s.module.name_ru, "protected": s.module.protected,
                    "can": [f"{s.module.tx_id:03X}", f"{s.module.rx_id:03X}"],
                    "ident": ident.to_dict(), "coding": coding, "channels": channels}
        return self.run(f"чтение блока {addr}…", go)

    def coding_write(self, addr: str, body) -> dict:
        v = self._vehicle()

        def go():
            s = v.module(addr)
            cur = s.read_coding()
            new = cur.copy()
            if "hex" in body:
                from ..vag.coding import LongCoding
                new = LongCoding(parse_hex(body["hex"]))
            for ch in body.get("bits", []):
                new.set_bit(int(ch["byte"]), int(ch["bit"]), int(ch["value"]))
            if len(new) != len(cur):
                raise ApiError("длина кодировки не совпадает")
            diff = [str(c) for c in cur.diff(new)]
            if body.get("dry_run", True):
                return {"dry_run": True, "old": cur.hex(), "new": new.hex(), "diff": diff}
            r = s.write_coding(new, reason="web coding", login=body.get("login"), progress=self.progress)
            return {"dry_run": False, "result": r.to_dict(), "diff": diff}
        return self.run(f"кодирование {addr}…", go)

    def channel_write(self, addr: str, body) -> dict:
        v = self._vehicle()

        def go():
            s = v.module(addr)
            ident = s.ident()
            ch = self.app.labels.find(s.module.addr_hex, ident.odx_dataset, body["name"])
            if ch is None:
                raise ApiError("канал неизвестен")
            cur = s.read_did(ch.did)
            new = ch.insert(cur, ch.resolve_value(body["value"]))
            info = {"did": did_key(ch.did), "old": cur.hex().upper(), "new": new.hex().upper(),
                    "old_value": ch.value_name(ch.extract(cur)), "new_value": ch.value_name(ch.extract(new))}
            if body.get("dry_run", True):
                return {"dry_run": True, **info}
            r = s.write({ch.did: new}, reason=f"web channel {ch.name}", login=body.get("login"),
                        session=ch.session or 0x03, progress=self.progress)
            return {"dry_run": False, **info, "result": r.to_dict()}
        return self.run(f"адаптация {addr}…", go)

    def did_read(self, addr: str, body) -> dict:
        v = self._vehicle()

        def go():
            did = parse_int(body["did"], bits=16)
            data = v.module(addr).read_did(did)
            return {"did": did_key(did), "hex": data.hex().upper(), "length": len(data),
                    "text": "".join(chr(b) if 32 <= b < 127 else "." for b in data)}
        return self.run(f"чтение DID {addr}…", go)

    def did_write(self, addr: str, body) -> dict:
        v = self._vehicle()

        def go():
            did = parse_int(body["did"], bits=16)
            data = parse_hex(body["hex"])
            s = v.module(addr)
            if body.get("confirm") is not True:
                cur = s.read_did(did)
                return {"dry_run": True, "did": did_key(did), "old": cur.hex().upper(), "new": data.hex().upper()}
            session = parse_int(body["session"]) if body.get("session") else 0x03
            login = parse_int(body["login"]) if body.get("login") else None
            r = s.write({did: data}, reason=f"web did {did_key(did)}", login=login, session=session,
                        progress=self.progress)
            return {"dry_run": False, "result": r.to_dict()}
        return self.run(f"запись DID {addr}…", go)

    def backup_all(self, _body) -> dict:
        v = self._vehicle()

        def go():
            from ..vag.learn import IGNORED_DIDS, load_known_dids
            found = [e for e in (v.found or v.scan(progress=self.progress)) if e.found]
            vin = v.vin or v.read_vin() or "UNKNOWN"
            saved = []
            for e in found:
                s = v.module(e.module.addr_hex)
                self.progress(f"копия {s.module.label()}…")
                try:
                    ident = s.ident(refresh=True)
                except (TimeoutError_, NegativeResponse):
                    continue
                dids = {"0x0600": ident.coding} if ident.coding else {}
                for k in load_known_dids(vin, s.module.addr_hex, ident.odx_dataset).get("dids", {}):
                    if int(k, 16) in IGNORED_DIDS:
                        continue
                    try:
                        dids[k] = s.read_did(int(k, 16)).hex().upper()
                    except (TimeoutError_, NegativeResponse):
                        continue
                b = self.app.store.save_backup(vin=vin, module=s.module.addr_hex, ident=ident.to_dict(),
                                               coding=ident.coding, dids=dids, reason="full backup")
                saved.append({"module": s.module.addr_hex, "values": len(dids), "path": b.path})
            return {"saved": saved}
        return self.run("копия всех блоков…", go)

    def recipes(self, q) -> dict:
        lang = (q.get("lang") or ["ru"])[0]
        return {"recipes": [r.to_summary(lang) for r in self.app.book.list()]}

    def recipe_plan(self, rid: str, body) -> dict:
        v = self._vehicle()
        r = self.app.book.get(rid)
        return self.run(f"проверка «{r.title.get('ru', rid)}»…",
                        lambda: plan_recipe(v, r, body.get("action", "on"), self.app.labels).to_dict())

    def recipe_states(self, _body) -> dict:
        v = self._vehicle()

        def go():
            out = {}
            for r in self.app.book.list():
                try:
                    p = plan_recipe(v, r, "on", self.app.labels)
                    if p.blockers:
                        needs = any("канал" in b for b in p.blockers)
                        out[r.id] = "needs-learning" if needs else "unavailable"
                    else:
                        out[r.id] = p.state
                except VagOnError:
                    out[r.id] = "error"
            return {"states": out}
        return self.run("проверка рецептов…", go)

    def recipe_apply(self, rid: str, body) -> dict:
        v = self._vehicle()
        r = self.app.book.get(rid)
        if body.get("confirm") is not True:
            raise ApiError("нужно подтверждение")

        def go():
            plan = plan_recipe(v, r, body.get("action", "on"), self.app.labels)
            results = apply_plan(v, plan, progress=self.progress)
            return {"plan": plan.to_dict(), "results": [x.to_dict() for x in results],
                    "after": r.to_summary().get("after")}
        return self.run(f"применяю «{r.title.get('ru', rid)}»…", go)

    def dtc(self, body) -> dict:
        v = self._vehicle()

        def go():
            mods = body.get("modules") or [e.module.addr_hex for e in (v.found or v.scan(progress=self.progress))
                                             if e.found]
            res = []
            for m in mods:
                s = v.module(m)
                self.progress(f"ошибки {s.module.label()}…")
                try:
                    dtcs = s.read_dtcs()
                except (TimeoutError_, NegativeResponse) as e:
                    res.append({"address": s.module.addr_hex, "name": s.module.name_ru, "error": str(e)})
                    continue
                res.append({"address": s.module.addr_hex, "name": s.module.name_ru, "dtcs": [
                    {"code": format_dtc(c), "raw": f"{c:06X}", "status": st, "status_text": describe_status(st)}
                    for c, st in dtcs]})
            return {"modules": res}
        return self.run("чтение ошибок…", go)

    def dtc_clear(self, body) -> dict:
        v = self._vehicle()
        if body.get("confirm") is not True:
            raise ApiError("нужно подтверждение")
        return self.run("стираю ошибки…", lambda: (v.module(body["module"]).clear_dtcs(), {"ok": True})[1])

    def backups(self, q) -> dict:
        mod = (q.get("module") or [None])[0]
        return {"backups": [{"path": b.path, "created": b.created, "module": b.module,
                             "reason": b.data.get("reason", ""), "vin": b.data.get("vin", "")}
                            for b in reversed(self.app.store.list_backups(module=mod))]}

    def restore(self, body) -> dict:
        v = self._vehicle()
        from ..vag.store import Store
        root = os.path.realpath(self.app.store.user_dir("backups"))
        path = os.path.realpath(str(body.get("path", "")))
        if os.path.commonpath([root, path]) != root:
            raise ApiError("копия должна лежать в папке резервных копий VagOn")
        b = Store.load_backup(path)

        def go():
            s = v.module(b.module)
            dry = body.get("confirm") is not True
            r = s.restore(b, dry_run=dry, progress=self.progress, coding_only=bool(body.get("coding_only")))
            return {"dry_run": dry, "result": r.to_dict()}
        return self.run(f"восстановление {b.module}…", go)

    def learn_start(self, body) -> dict:
        v = self._vehicle()
        from ..vag.learn import LearnSession, parse_ranges, sweep

        def go():
            s = v.module(body["module"])
            s.ident(refresh=True)
            vin = v.vin or v.read_vin() or "UNKNOWN"

            def prog(i, n, found):
                if i % 32 == 0 or i == n:
                    self.progress(f"поиск идентификаторов {i}/{n}, найдено {found}")
            # resumes an interrupted discovery; returns at once when it is complete
            data = sweep(s, parse_ranges(body.get("ranges")), vin=vin, progress=prog)
            dids = [int(k, 16) for k in data["dids"]]
            self.progress("снимок значений…")
            ls = LearnSession.start(s, dids, vin)
            return {"count": len(ls.data["before"]), "noisy": len(ls.data["noisy"])}
        return self.run("обучение: снимок…", go)

    def learn_finish(self, body) -> dict:
        v = self._vehicle()
        from ..vag.labels import save_user_channel
        from ..vag.learn import LearnSession, infer_channel

        def go():
            s = v.module(body["module"])
            ls = LearnSession.load(s.module.addr_hex)
            changes = ls.finish(s)
            res = {"changes": [{"did": did_key(c.did), "old": c.old.hex().upper(), "new": c.new.hex().upper(),
                                "bits": c.changed_bits()} for c in changes]}
            ident = s.ident(refresh=True)
            if body.get("name") and len(changes) == 1:
                ch = infer_channel(changes[0], body["name"], body.get("old", ""), body.get("new", ""),
                                   source=f"learn {time.strftime('%Y-%m-%d')}")
                res["label_file"] = save_user_channel(s.module.addr_hex, ident.odx_dataset, ch)
                res["channel"] = ch.to_json()
            elif body.get("name") and len(changes) > 1:
                res["warning"] = "изменилось несколько идентификаторов — метка не сохранена; сохраните как рецепт"
            if body.get("recipe_name") and changes:
                from ..cli import slug
                rid = f"user.{slug(body['recipe_name'])}"
                rec = build_learned_recipe(rid, body["recipe_name"], s.module.addr_hex, changes, ident)
                save_user_recipe(rec)
                res["recipe"] = rid
            if res.get("channel") or res.get("recipe") or not changes:
                ls.commit()
            self.app.reload()
            return res
        return self.run("обучение: сравнение…", go)

    def journal(self, _q) -> dict:
        return {"entries": list(reversed(self.app.store.read_journal(100)))}

    def log(self, _q) -> dict:
        path = self.app.trace.path if self.app.trace else None
        if not path or not os.path.exists(path):
            return {"path": None, "lines": []}
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()[-200:]
        return {"path": path, "lines": [ln.rstrip("\n") for ln in lines]}

    def modules_table(self, _q) -> dict:
        return {"modules": [{"address": m.addr_hex, "name": m.name_ru, "protected": m.protected}
                            for m in MODULES]}

    def sim_change(self, body) -> dict:
        """Simulator only: emulate a change made by another tool (for trying learn mode)."""
        if not self.app.sim or self.app.transport is None:
            raise ApiError("только в режиме симулятора")
        sim = self.app.transport.link.elm  # type: ignore[attr-defined]
        m = get_module(body["module"])
        ecu = next(e for e in sim.ecus if e.address == m.address)
        did = parse_int(body["did"], bits=16)
        data = parse_hex(body["hex"])
        if did not in ecu.dids or len(ecu.dids[did]) != len(data):
            raise ApiError("нет такого DID или другая длина")
        ecu.dids[did] = data
        if sim.on_change:
            sim.on_change()
        return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    api: Api = None  # set by serve()
    server_version = f"VagOn/{__version__}"

    def log_message(self, fmt, *args):  # keep the console quiet
        pass

    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        """DNS-rebinding defence: only answer requests addressed to this machine by IP/localhost."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host.startswith("["):
            name = host.split("]")[0] + "]"
        else:
            name = host.rsplit(":", 1)[0] if ":" in host else host
        if name == "localhost":
            return True
        # DNS rebinding needs a domain name; an IP literal (127.0.0.1, the Mac's LAN IP…) is safe
        import ipaddress
        try:
            ipaddress.ip_address(name.strip("[]"))
            return True
        except ValueError:
            return False

    def do_GET(self):
        if not self._host_ok():
            self._json(403, {"error": "неверный Host"})
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        if path in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                body = f.read()
            self._send(200, body, "text/html; charset=utf-8")
            return
        routes = {
            "/api/status": self.api.status, "/api/recipes": self.api.recipes,
            "/api/backups": self.api.backups, "/api/journal": self.api.journal, "/api/log": self.api.log,
            "/api/modules": self.api.modules_table,
        }
        self._dispatch(lambda: routes[path](q) if path in routes else self._module_get(path, q))

    def _module_get(self, path, q):
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["api", "module"]:
            return self.api.module(parts[2], q)
        raise ApiError("не найдено", 404)

    def do_POST(self):
        if not self._host_ok():
            self._json(403, {"error": "неверный Host"})
            return
        if self.headers.get("X-VagOn") != "1":
            self._json(403, {"error": "запрос без заголовка X-VagOn отклонён"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ApiError("слишком большой запрос", 413)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not isinstance(body, dict):
                raise ApiError("ожидался JSON-объект")
        except (ValueError, ApiError) as e:
            self._json(400, {"error": str(e)})
            return
        path = urlparse(self.path).path
        parts = path.strip("/").split("/")
        a = self.api
        simple = {
            "/api/connect": a.connect, "/api/disconnect": a.disconnect, "/api/scan": a.scan,
            "/api/dtc": a.dtc, "/api/dtc/clear": a.dtc_clear, "/api/restore": a.restore,
            "/api/learn/start": a.learn_start, "/api/learn/finish": a.learn_finish,
            "/api/recipes/states": a.recipe_states, "/api/sim/change": a.sim_change,
            "/api/backup_all": a.backup_all,
        }
        if path in simple:
            self._dispatch(lambda: simple[path](body))
        elif len(parts) == 4 and parts[:2] == ["api", "recipes"] and parts[3] in ("plan", "apply"):
            fn = a.recipe_plan if parts[3] == "plan" else a.recipe_apply
            self._dispatch(lambda: fn(parts[2], body))
        elif len(parts) == 4 and parts[:2] == ["api", "module"] and parts[3] in ("coding", "channel",
                                                                                   "did_read", "did_write"):
            fn = {"coding": a.coding_write, "channel": a.channel_write, "did_read": a.did_read,
                  "did_write": a.did_write}[parts[3]]
            self._dispatch(lambda: fn(parts[2], body))
        else:
            self._json(404, {"error": "не найдено"})

    def _dispatch(self, fn):
        try:
            self._json(200, fn())
        except ApiError as e:
            self._json(e.status, {"error": str(e)})
        except (VagOnError, KeyError, ValueError, IndexError) as e:
            msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
            self._json(400, {"error": str(msg)})
        except Exception as e:  # never kill the server thread silently
            traceback.print_exc()
            self._json(500, {"error": f"внутренняя ошибка: {e!r}"})


def make_server(app: App, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"api": Api(app)})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def serve(app: App, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False) -> None:
    srv = make_server(app, host, port)
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{port}/"
    print(f"VagOn веб-интерфейс: {url}  (Ctrl+C — выход)", flush=True)
    if host not in ("127.0.0.1", "localhost"):
        print("⚠ Сервер доступен из сети. Любой в этой сети сможет управлять адаптером.", flush=True)
    if open_browser:
        import webbrowser
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        app.disconnect()
