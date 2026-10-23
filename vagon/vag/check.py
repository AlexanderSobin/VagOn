"""Adapter suitability check for VAG coding (read-only)."""

from __future__ import annotations

from ..errors import NegativeResponse, TransportError, VagOnError
from .ident import DID_CODING, DID_SPARE_PART, decode_text


def adapter_check(vehicle) -> dict:
    """Run read-only probes and report what works.

    * gateway (19) answers → CAN wiring/ignition OK
    * multi-frame read (F187 is 14 bytes with the header) → flow control to the
      right CAN ID works (ATFCSH/ATFCSM1)
    * long coding read → the most common long answer works
    Writes need manual multi-frame transmit (ATCAF0/ATR0/ATST); those commands
    were already exercised when the adapter was configured.
    """
    res = {"bus": False, "multiframe": False, "coding_read": False, "voltage": None,
           "stn": bool(getattr(vehicle.transport, "is_stn", False)), "notes": []}
    t = vehicle.transport
    try:
        res["voltage"] = t.read_voltage() if hasattr(t, "read_voltage") else None
    except VagOnError:
        pass
    if res["voltage"] is not None and res["voltage"] < 12.0:
        res["notes"].append(f"напряжение {res['voltage']:.1f} В — для записи подключите зарядное")
    for addr in ("19", "09", "17"):
        sess = vehicle.module(addr)
        try:
            part = decode_text(sess.read_did(DID_SPARE_PART))
        except NegativeResponse:
            res["bus"] = True
            continue
        except VagOnError as e:
            msg = str(e)
            if "оборван" in msg or "ATFCSM1" in msg:
                res["bus"] = True
                res["notes"].append("длинные ответы не доходят: адаптер не умеет flow control на ID "
                                    "блока VAG (нужны ATFCSH/ATFCSM1)")
                break
            continue
        except TransportError as e:
            res["notes"].append(str(e))
            break
        res["bus"] = res["multiframe"] = True
        res["probe_module"] = addr
        res["probe_part"] = part
        try:
            sess.read_did(DID_CODING)
            res["coding_read"] = True
        except NegativeResponse:
            res["coding_read"] = True   # module answered; it simply has no long coding
        except VagOnError as e:
            res["notes"].append(f"чтение кодирования: {e}")
        break
    if not res["bus"]:
        res["notes"].append("ни шлюз (19), ни блоки 09/17 не ответили — включите зажигание, проверьте адаптер")
    if res["multiframe"] and not getattr(t, "_fc_ok", True):
        res["notes"].append("адаптер не поддерживает ATFCSM1 — VagOn сам отвечает Flow Control "
                            "(работает, но чтение длинных ответов медленнее)")
    res["ok"] = res["bus"] and res["multiframe"] and res["coding_read"]
    return res


def format_check(res: dict) -> list[str]:
    yes, no = "✔", "✖"
    lines = [
        f"{yes if res['bus'] else no} связь с шиной CAN",
        f"{yes if res['multiframe'] else no} длинные ответы (flow control VAG)",
        f"{yes if res['coding_read'] else no} чтение длинного кодирования",
    ]
    if res.get("stn"):
        lines.append(f"{yes} чип STN — длинные запросы через STPX")
    lines += [f"⚠ {n}" for n in res["notes"]]
    lines.append("Итог: " + ("адаптер подходит для кодирования VAG" if res["ok"] else
                             "адаптер НЕ подходит или машина не отвечает"))
    return lines
