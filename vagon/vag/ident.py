"""Module identification (the 'Identification' screen of ODIS/VCDS)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..errors import NegativeResponse, TimeoutError_
from ..uds import UdsClient

DID_CODING = 0x0600
DID_SPARE_PART = 0xF187
DID_SW_VERSION = 0xF189
DID_HW_NUMBER = 0xF191
DID_HW_VERSION = 0xF1A3
DID_SYSTEM_NAME = 0xF197
DID_ODX_FILE = 0xF19E
DID_ODX_VERSION = 0xF1A2
DID_SERIAL = 0xF18C
DID_VIN = 0xF190
DID_WORKSHOP_CODE = 0xF1A5      # coding fingerprint (read-only, filled from F198)
DID_TESTER_ID = 0xF198          # repair shop code / tester serial (write before coding)
DID_PROG_DATE = 0xF199          # programming date, BCD YY MM DD (write before coding)
DID_ACTIVE_SESSION = 0xF186

TEXT_DIDS = [
    ("part_number", DID_SPARE_PART),
    ("sw_version", DID_SW_VERSION),
    ("hw_number", DID_HW_NUMBER),
    ("hw_version", DID_HW_VERSION),
    ("system_name", DID_SYSTEM_NAME),
    ("odx_dataset", DID_ODX_FILE),
    ("odx_version", DID_ODX_VERSION),
    ("serial", DID_SERIAL),
    ("vin", DID_VIN),
]


def decode_text(data: bytes) -> str:
    return data.split(b"\x00", 1)[0].decode("latin-1", "replace").strip()


@dataclass
class Identification:
    address: str
    part_number: str = ""
    sw_version: str = ""
    hw_number: str = ""
    hw_version: str = ""
    system_name: str = ""
    odx_dataset: str = ""
    odx_version: str = ""
    serial: str = ""
    vin: str = ""
    coding: str | None = None        # hex, None if the module has no long coding
    workshop_code: str | None = None
    errors: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        parts = [self.part_number, self.system_name, f"SW {self.sw_version}" if self.sw_version else ""]
        return "  ".join(p for p in parts if p)


def read_identification(uds: UdsClient, address: str, *, with_coding: bool = True,
                        quick: bool = False) -> Identification:
    ident = Identification(address=address)
    dids = TEXT_DIDS if not quick else [("part_number", DID_SPARE_PART),
                                        ("system_name", DID_SYSTEM_NAME),
                                        ("sw_version", DID_SW_VERSION),
                                        ("odx_dataset", DID_ODX_FILE)]
    for attr, did in dids:
        try:
            setattr(ident, attr, decode_text(uds.read_did(did)))
        except NegativeResponse as e:
            ident.errors[attr] = f"NRC 0x{e.nrc:02X}"
        except TimeoutError_ as e:
            ident.errors[attr] = str(e)
            if attr == "part_number":
                raise
    if with_coding:
        try:
            ident.coding = uds.read_did(DID_CODING).hex().upper()
        except NegativeResponse as e:
            ident.errors["coding"] = f"NRC 0x{e.nrc:02X}"
        try:
            ident.workshop_code = uds.read_did(DID_WORKSHOP_CODE).hex().upper()
        except (NegativeResponse, TimeoutError_):
            pass
    return ident
