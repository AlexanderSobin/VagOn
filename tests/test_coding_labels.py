import json

import pytest

from vagon.vag.coding import LongCoding
from vagon.vag.labels import Channel, LabelDB, save_user_channel
from vagon.vag.learn import DidChange, infer_channel


def test_coding_bits():
    lc = LongCoding(bytes([0x00, 0xFF]))
    lc.set_bit(0, 3, 1)
    lc.set_bit(1, 0, 0)
    assert bytes(lc) == bytes([0x08, 0xFE])
    assert lc.get_bit(0, 3) == 1 and lc.get_bit(1, 0) == 0
    lc.set_field(1, 0x0C, 0x04)
    assert lc.data[1] == 0xF6
    with pytest.raises(ValueError):
        lc.set_field(1, 0x0C, 0x01)
    with pytest.raises(IndexError):
        lc.set_bit(2, 0, 1)
    with pytest.raises(IndexError):
        lc.set_bit(0, 8, 1)


def test_coding_diff():
    a = LongCoding(bytes([0b0000_0001, 0x00]))
    b = LongCoding(bytes([0b1000_0000, 0x00]))
    d = a.diff(b)
    assert [(c.byte, c.bit, c.old, c.new) for c in d] == [(0, 0, 1, 0), (0, 7, 0, 1)]


def test_channel_single_byte():
    ch = Channel("x", 0x0901, byte=1, bit=2, bits=3, values={"a": 0, "b": 5})
    data = bytes([0xFF, 0b1110_0011, 0x00])
    assert ch.extract(data) == 0
    new = ch.insert(data, 5)
    assert new == bytes([0xFF, 0b1111_0111, 0x00])
    assert ch.extract(new) == 5
    assert ch.value_name(5) == "b"
    assert ch.resolve_value("B") == 5
    with pytest.raises(ValueError):
        ch.insert(data, 8)


def test_channel_multi_byte_and_scale():
    ch = Channel("v", 0x0905, byte=0, bit=0, bits=16, scale=0.01, offset=-10.0, unit="V")
    data = bytes([0x00, 0x00])
    raw = ch.resolve_value("12.5")
    assert raw == 2250
    new = ch.insert(data, raw)
    assert new == (2250).to_bytes(2, "big")
    assert ch.value_name(ch.extract(new)) == "12.5V"
    span = Channel("s", 1, byte=0, bit=4, bits=8)   # spans two bytes
    d = span.insert(bytes(2), 0xAB)
    assert d == bytes([0x0A, 0xB0]) and span.extract(d) == 0xAB


def test_channel_out_of_range():
    ch = Channel("x", 1, byte=1, bit=0, bits=8)
    with pytest.raises(ValueError):
        ch.extract(b"\x00")


def test_labeldb_prefix_and_duplicates(tmp_path):
    db = LabelDB()
    db.load_dict({"vagon_labels": 1, "datasets": {
        "EV_A": {"module": "5F", "channels": [{"name": "Foo bar", "did": "0x0100", "bits": 1}]},
        "EV_AB": {"module": "5F", "channels": [{"name": "foo_bar", "did": "0x0200", "bits": 1}]},
    }})
    assert db.find("5F", "EV_ABC", "FOO BAR").did == 0x0200    # most specific wins
    assert db.find("5F", "EV_AX", "foo-bar").did == 0x0100
    assert db.find("09", "EV_ABC", "foo bar") is None             # other module
    with pytest.raises(ValueError):
        db.load_dict({"vagon_labels": 1, "datasets": {"X": {"channels": [
            {"name": "a b", "did": 1}, {"name": "a_b", "did": 2}]}}})
    with pytest.raises(ValueError):
        db.load_dict({"nope": 1})


def test_save_user_channel_roundtrip():
    path = save_user_channel("09", "EV_TEST", Channel("Coming home", 0x0901, values={"off": 0, "on": 1}))
    save_user_channel("09", "EV_TEST", Channel("Coming home", 0x0902, values={"off": 0, "on": 1}))
    data = json.load(open(path, encoding="utf-8"))
    chans = data["datasets"]["EV_TEST"]["channels"]
    assert len(chans) == 1 and chans[0]["did"] == "0x0902"
    db = LabelDB.load_default()
    assert db.find("09", "EV_TEST_SUFFIX", "coming home").did == 0x0902


def test_infer_channel_enum_numeric_multibyte():
    ch = infer_channel(DidChange(1, b"\x0F", b"\x0B"), "flag", "active", "not active")
    assert (ch.byte, ch.bit, ch.bits) == (0, 2, 1)
    assert ch.values == {"active": 1, "not active": 0}
    num = infer_channel(DidChange(2, b"\x01", b"\x03"), "duration", "10", "30")
    assert (num.bit, num.bits) == (0, 8) and num.scale == pytest.approx(10) and num.offset == pytest.approx(0)
    volt = infer_channel(DidChange(3, bytes([76]), bytes([121])), "volt", "7.6", "12.1")
    assert volt.resolve_value("12.1") == 121
    wide = infer_channel(DidChange(4, b"\x00\x01\x00", b"\x01\x00\x00"), "w", "a", "b")
    assert (wide.byte, wide.bit, wide.bits) == (0, 0, 9)
    assert wide.extract(b"\x01\x00\x00") == 0x100 and wide.extract(b"\x00\x01\x00") == 0x001
