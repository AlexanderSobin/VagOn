import pytest

from vagon.errors import NegativeResponse, ResponsePendingTimeout, TimeoutError_
from vagon.transport.base import Channel
from vagon.uds import UdsClient


class FakeChannel(Channel):
    tx_id, rx_id = 0x70E, 0x778

    def __init__(self, script):
        self.script = list(script)      # list of lists of responses per exchange
        self.listen_script = []
        self.sent = []

    def exchange(self, payload, timeout):
        self.sent.append(payload)
        return self.script.pop(0) if self.script else []

    def listen(self, timeout):
        return self.listen_script.pop(0) if self.listen_script else []


def test_read_did_ok():
    ch = FakeChannel([[b"\x62\xF1\x87ABC"]])
    assert UdsClient(ch).read_did(0xF187) == b"ABC"
    assert ch.sent == [b"\x22\xF1\x87"]


def test_wrong_did_echo_rejected():
    ch = FakeChannel([[b"\x62\xF1\x88ABC"]])
    with pytest.raises(Exception):
        UdsClient(ch).read_did(0xF187)


def test_negative_response():
    ch = FakeChannel([[b"\x7F\x22\x31"]])
    with pytest.raises(NegativeResponse) as e:
        UdsClient(ch).read_did(0x1234)
    assert e.value.nrc == 0x31 and "0x31" in str(e.value)


def test_pending_then_final_in_same_exchange():
    ch = FakeChannel([[b"\x7F\x2E\x78", b"\x7F\x2E\x78", b"\x6E\x06\x00"]])
    UdsClient(ch).write_did(0x0600, b"\x01")


def test_pending_then_listen():
    ch = FakeChannel([[b"\x7F\x2E\x78"]])
    ch.listen_script = [[b"\x6E\x06\x00"]]
    UdsClient(ch, pending_timeout=1).write_did(0x0600, b"\x01")


def test_pending_never_answers():
    ch = FakeChannel([[b"\x7F\x2E\x78"]])
    with pytest.raises(ResponsePendingTimeout):
        UdsClient(ch, pending_timeout=0.2).write_did(0x0600, b"\x01")


def test_busy_retry():
    ch = FakeChannel([[b"\x7F\x22\x21"], [b"\x62\xF1\x87X"]])
    assert UdsClient(ch).read_did(0xF187) == b"X"


def test_stray_reply_ignored_then_timeout():
    ch = FakeChannel([[b"\x62\x01\x02\x03"]])
    with pytest.raises(TimeoutError_):
        UdsClient(ch).tester_present()


def test_session_timing_parsed():
    ch = FakeChannel([[b"\x50\x03\x00\x32\x01\xF4"]])
    u = UdsClient(ch)
    u.diagnostic_session(3)
    assert u.session == 3 and u.timing.p2 == 0.05 and u.timing.p2_star == 5.0


def test_dtc_parse():
    ch = FakeChannel([[b"\x59\x02\xFF\x9A\x0B\x00\x08\x11\x22\x33\x2F"]])
    avail, dtcs = UdsClient(ch).read_dtc_by_status()
    assert avail == 0xFF and dtcs == [(0x9A0B00, 0x08), (0x112233, 0x2F)]


@pytest.mark.parametrize("payload", [b"\x10\x02", b"\x10\x82", b"\x34\x00\x44", b"\x36\x01", b"\x3D\x14",
                                     b"\x28\x03\x01", b""])
def test_policy_blocks_flashing(payload):
    from vagon.errors import SafetyError
    ch = FakeChannel([[b"\x50\x02"]])
    with pytest.raises(SafetyError):
        UdsClient(ch).request(payload)
    assert ch.sent == [], "refused requests must never reach the adapter"


def test_policy_allows_dev_session():
    ch = FakeChannel([[b"\x50\x4F\x00\x32\x01\xF4"]])
    UdsClient(ch).diagnostic_session(0x4F)


def test_late_answer_for_other_did_skipped():
    ch = FakeChannel([[b"\x6E\x09\x60", b"\x6E\x09\x61"]])
    UdsClient(ch).write_did(0x0961, b"\x01")
    ch2 = FakeChannel([[b"\x62\x06\x00\x01", b"\x62\xF1\x87ABC"]])
    assert UdsClient(ch2).read_did(0xF187) == b"ABC"
    ch3 = FakeChannel([[b"\x67\x03\x00\x00\x00\x00"]])
    with pytest.raises(TimeoutError_):
        UdsClient(ch3).send_key(0x03, b"\x00\x00\x00\x01")     # 67 03 isn't an answer to 27 04
