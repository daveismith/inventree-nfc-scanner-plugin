"""The NDEF message for a bin's tag, byte for byte."""

import pytest

from inventree_nfc_scanner import ndef


def test_location_text_and_uri():
    assert ndef.location_text(42) == "INV-SL42"
    assert (
        ndef.location_uri("https://inv.example.com/", 42)
        == "https://inv.example.com/web/stock/location/42"
    )


def test_message_bytes():
    uri = b"\x04inv.example.com/web/stock/location/42"
    text = b"\x02enINV-SL42"
    expected = (
        bytes([0x91, 1, len(uri)])
        + b"U"
        + uri  # MB, SR, well-known
        + bytes([0x51, 1, len(text)])
        + b"T"
        + text  # ME, SR, well-known
    )
    assert ndef.build_message("https://inv.example.com", 42) == expected


def test_http_and_a_path_prefix():
    msg = ndef.build_message("http://host:8080/inventree/", 7)
    assert msg[4] == 0x03  # http://
    assert b"host:8080/inventree/web/stock/location/7" in msg


@pytest.mark.parametrize("base", ["ftp://host", "file:///x"])
def test_unsupported_scheme(base):
    with pytest.raises(ValueError, match="unsupported scheme"):
        ndef.build_message(base, 1)


def test_payload_over_255_bytes():
    with pytest.raises(ValueError, match="255"):
        ndef.build_message("https://" + "h" * 260, 1)
