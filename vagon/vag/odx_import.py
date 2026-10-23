"""Import adaptation channel labels from standard ODX / PDX files (ISO 22901).

Needs the open-source `odxtools` (Mercedes-Benz): `pip3 install odxtools` — on the
Mac only; the resulting JSON label file also works on iPhone. Only plain,
unencrypted ODX is supported (ODIS' own data is encrypted and VagOn does not
decrypt it).

Every ReadDataByIdentifier service (request = SID 0x22 + 16-bit DID constant)
becomes one channel per value parameter of its positive response:
  byte = BYTE-POSITION - 3 (after 62 + DID), bit = BIT-POSITION, bits = BIT-LENGTH,
  TEXTTABLE → named values, LINEAR → scale/offset.
The ECU-variant / base-variant short name (EV_…/BV_…) is the dataset key, which
VagOn matches against the module's F19E.
"""

from __future__ import annotations

import json
import os

from ..errors import VagOnError
from ..paths import sub_dir
from .labels import norm_name


def _coded_const(p):
    return type(p).__name__ == "CodedConstParameter"


def _bit_length(obj) -> int | None:
    dct = getattr(obj, "diag_coded_type", None)
    return getattr(dct, "bit_length", None)


def read_did_of(service) -> int | None:
    req = getattr(service, "request", None)
    params = list(getattr(req, "parameters", []) or [])
    if len(params) < 2 or not _coded_const(params[0]) or not _coded_const(params[1]):
        return None
    if params[0].coded_value != 0x22 or (params[0].byte_position or 0) != 0:
        return None
    if params[1].byte_position != 1 or _bit_length(params[1]) != 16:
        return None
    return int(params[1].coded_value)


def _limit_value(limit):
    raw = getattr(limit, "value_raw", None)
    if raw is None:
        raw = getattr(limit, "value", None)
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def compu_info(dop) -> dict:
    """{'values': {...}} for TEXTTABLE, {'scale', 'offset'} for LINEAR, {} otherwise."""
    cm = getattr(dop, "compu_method", None)
    if cm is None:
        return {}
    cat = str(getattr(getattr(cm, "category", None), "value", getattr(cm, "category", ""))).upper()
    itp = getattr(cm, "compu_internal_to_phys", None)
    scales = list(getattr(itp, "compu_scales", []) or [])
    if "TEXTTABLE" in cat:
        values = {}
        for sc in scales:
            lo, hi = _limit_value(sc.lower_limit), _limit_value(getattr(sc, "upper_limit", None) or sc.lower_limit)
            text = getattr(getattr(sc, "compu_const", None), "vt", None)
            if lo is None or text is None or (hi is not None and hi != lo):
                continue
            values.setdefault(str(text), lo)
        return {"values": values} if values else {}
    if "LINEAR" in cat and scales:
        coeffs = getattr(scales[0], "compu_rational_coeffs", None)
        nums = list(getattr(coeffs, "numerators", []) or [])
        dens = list(getattr(coeffs, "denominators", []) or [1])
        if len(nums) >= 2:
            den = float(dens[0]) or 1.0
            return {"scale": float(nums[1]) / den, "offset": float(nums[0]) / den}
    return {}


def convert_layer(layer, module: str = "") -> list[dict]:
    channels: list[dict] = []
    used: set[str] = set()
    for svc in getattr(layer, "services", []) or []:
        did = read_did_of(svc)
        if did is None:
            continue
        responses = list(getattr(svc, "positive_responses", []) or [])
        if not responses:
            continue
        values_params = [p for p in responses[0].parameters if type(p).__name__ == "ValueParameter"]
        for p in values_params:
            bp = getattr(p, "byte_position", None)
            dop = getattr(p, "dop", None)
            bits = _bit_length(dop)
            if bp is None or bp < 3 or not bits or bits > 64:
                continue
            names = []
            for n in (getattr(p, "long_name", None),
                      getattr(svc, "long_name", None) if len(values_params) == 1 else None,
                      getattr(p, "short_name", None), getattr(svc, "short_name", None)
                      if len(values_params) == 1 else None):
                if n and norm_name(n) not in [norm_name(x) for x in names]:
                    names.append(str(n))
            info = compu_info(dop)
            base = {"did": f"0x{did:04X}", "byte": bp - 3, "bit": getattr(p, "bit_position", None) or 0,
                    "bits": bits, "values": info.get("values", {}), "source": "odx"}
            if "scale" in info:
                base["scale"] = info["scale"]
                base["offset"] = info["offset"]
            for name in names:
                key = norm_name(name)
                if key in used:
                    name = f"{name} ({base['did']})"
                    key = norm_name(name)
                    if key in used:
                        continue
                used.add(key)
                channels.append({"name": name, **base})
    return channels


def convert_database(db, module: str = "") -> dict:
    datasets = {}
    for layer in getattr(db, "diag_layers", []) or []:
        chans = convert_layer(layer, module)
        if chans:
            datasets[str(layer.short_name)] = {"module": module.upper() if module else "", "channels": chans}
    return {"vagon_labels": 1, "datasets": datasets}


def import_odx(path: str, module: str | None = None) -> tuple[str, int]:
    try:
        import odxtools  # type: ignore
    except ImportError as e:
        raise VagOnError("для импорта ODX нужен odxtools: pip3 install odxtools (на Mac)") from e
    if not os.path.exists(path):
        raise VagOnError(f"файл не найден: {path}")
    db = odxtools.load_pdx_file(path) if path.lower().endswith(".pdx") else odxtools.load_file(path)
    data = convert_database(db, module or "")
    n = sum(len(d["channels"]) for d in data["datasets"].values())
    out = os.path.join(sub_dir("labels"), "odx_" + os.path.splitext(os.path.basename(path))[0] + ".json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return out, n
