"""ODX → label conversion, tested on a duck-typed object model (odxtools optional)."""

import os
from types import SimpleNamespace as NS

import pytest

from vagon.vag.labels import LabelDB
from vagon.vag.odx_import import compu_info, convert_database, read_did_of


class CodedConstParameter(NS):
    pass


class ValueParameter(NS):
    pass


def const(value, byte, bits):
    return CodedConstParameter(coded_value=value, byte_position=byte, bit_position=None,
                               diag_coded_type=NS(bit_length=bits), short_name="c", long_name=None)


def texttable(pairs, bits=1):
    scales = [NS(lower_limit=NS(value_raw=str(v)), upper_limit=NS(value_raw=str(v)), compu_const=NS(vt=t))
              for t, v in pairs]
    return NS(diag_coded_type=NS(bit_length=bits),
              compu_method=NS(category=NS(value="TEXTTABLE"), compu_internal_to_phys=NS(compu_scales=scales)))


def linear(factor, offset, bits=8):
    sc = NS(compu_rational_coeffs=NS(numerators=[offset, factor], denominators=[1]),
            lower_limit=NS(value_raw="0"))
    return NS(diag_coded_type=NS(bit_length=bits),
              compu_method=NS(category=NS(value="LINEAR"), compu_internal_to_phys=NS(compu_scales=[sc])))


def service(did, params, long_name=None):
    req = NS(parameters=[const(0x22, 0, 8), const(did, 1, 16)])
    resp = NS(parameters=[const(0x62, 0, 8), const(did, 1, 16)] + params)
    return NS(short_name=f"Read_{did:04X}", long_name=long_name, request=req, positive_responses=[resp])


def test_read_did_detection():
    assert read_did_of(service(0x0961, [])) == 0x0961
    bad = NS(request=NS(parameters=[const(0x2E, 0, 8), const(0x0961, 1, 16)]))
    assert read_did_of(bad) is None
    short = NS(request=NS(parameters=[const(0x22, 0, 8), const(5, 1, 8)]))
    assert read_did_of(short) is None


def test_compu():
    assert compu_info(texttable([("not activated", 0), ("activated", 1)])) == \
        {"values": {"not activated": 0, "activated": 1}}
    assert compu_info(linear(0.1, 0)) == {"scale": 0.1, "offset": 0.0}


def test_convert_and_use_with_labeldb():
    layer = NS(short_name="EV_TEST_MIB3", services=[
        service(0x0960, [ValueParameter(short_name="apple", long_name="Apple_DIO_Wireless", byte_position=3,
                                        bit_position=1, dop=texttable([("not activated", 0), ("activated", 1)]))]),
        service(0x0905, [ValueParameter(short_name="vlim", long_name=None, byte_position=3, bit_position=0,
                                        dop=linear(0.1, 0.0))], long_name="Start_stop_voltage_limit"),
    ])
    data = convert_database(NS(diag_layers=[layer]), "5F")
    db = LabelDB()
    db.load_dict(data)
    ch = db.find("5F", "EV_TEST_MIB3_V001", "Apple_DIO_Wireless")
    assert (ch.did, ch.byte, ch.bit, ch.bits) == (0x0960, 0, 1, 1)
    assert ch.resolve_value("activated") == 1
    v = db.find("5F", "EV_TEST_MIB3", "Start_stop_voltage_limit")
    assert v.resolve_value("12.1") == 121
    assert db.find("5F", "EV_TEST_MIB3", "apple") is not None    # short name alias


@pytest.mark.skipif(not os.environ.get("ODX_SAMPLE"), reason="set ODX_SAMPLE=path/to/somersault.pdx")
def test_real_odxtools_smoke():
    from vagon.vag.odx_import import import_odx
    path, n = import_odx(os.environ["ODX_SAMPLE"])
    assert os.path.exists(path) and n >= 0
