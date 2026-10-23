import pytest

from vagon import isotp
from vagon.errors import IsoTpError
from vagon.hexutil import parse_hex, parse_int, to_hex


@pytest.mark.parametrize("n", [1, 6, 7, 8, 13, 14, 15, 62, 111, 112, 113, 500, 4095])
def test_roundtrip(n):
    payload = bytes((i * 7 + 3) & 0xFF for i in range(n))
    frames = isotp.encode(payload)
    assert all(len(f) == 8 for f in frames)
    r = isotp.Reassembler()
    out = [r.feed(f) for f in frames]
    assert out[-1] == payload
    assert all(o is None for o in out[:-1])


def test_single_frame_layout():
    assert isotp.encode(b"\x22\xF1\x87") == [b"\x03\x22\xF1\x87\x55\x55\x55\x55"]


def test_first_frame_layout_and_sequence_wrap():
    frames = isotp.encode(bytes(200))
    assert frames[0][:2] == b"\x10\xC8"
    sns = [f[0] & 0x0F for f in frames[1:]]
    assert sns[:16] == list(range(1, 16)) + [0]
    assert all(f[0] >> 4 == 2 for f in frames[1:])


def test_lost_frame_detected():
    frames = isotp.encode(bytes(30))
    r = isotp.Reassembler()
    r.feed(frames[0])
    r.feed(frames[1])
    with pytest.raises(IsoTpError):
        r.feed(frames[3])


def test_cf_without_ff():
    with pytest.raises(IsoTpError):
        isotp.Reassembler().feed(b"\x21" + bytes(7))


def test_flow_control():
    fc = isotp.parse_flow_control(b"\x30\x08\x14\xAA\xAA\xAA\xAA\xAA")
    assert (fc.status, fc.block_size, fc.st_min_s) == (0, 8, 0.020)
    assert isotp.parse_flow_control(b"\x30\x00\xF5").st_min_s == pytest.approx(0.0005)
    with pytest.raises(IsoTpError):
        isotp.parse_flow_control(b"\x35\x00\x00")


def test_too_long():
    with pytest.raises(IsoTpError):
        isotp.encode(bytes(4096))
    with pytest.raises(IsoTpError):
        isotp.encode(b"")


def test_parse_hex_variants():
    assert parse_hex("0A 1b") == b"\x0A\x1B"
    assert parse_hex("0a1b") == b"\x0A\x1B"
    assert parse_hex("0x0A,0x1B") == b"\x0A\x1B"
    assert parse_hex("A 1B") == b"\x0A\x1B"
    for bad in ("0G", "ABC", "--", "0x"):
        with pytest.raises(ValueError):
            parse_hex(bad)
    assert to_hex(b"\x01\xAB") == "01 AB"


def test_parse_int():
    assert parse_int("0x1A") == 26
    assert parse_int("1Ah") == 26
    assert parse_int("26") == 26
    assert parse_int("0b101") == 5
    assert parse_int(7) == 7
    with pytest.raises(ValueError):
        parse_int("0x100", bits=8)
    with pytest.raises(ValueError):
        parse_int("+5")
    with pytest.raises(ValueError):
        parse_int("")
