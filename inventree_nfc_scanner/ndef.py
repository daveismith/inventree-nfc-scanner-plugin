"""What goes on a bin's tag: one NDEF message with two records.

- A URI record, `https://<host>/web/stock/location/<pk>`, so a phone opens the location.
- A Text record, `INV-SL<pk>`, InvenTree's short barcode for the location, which the scanner
  types as a keyboard.

This is the one place the bytes are built. The USB route fetches them, the network route
sends them in the job. It is byte-for-byte the builder in the firmware repository's
`tools/nfcprog.py`, whose output the firmware's host tests check.
"""

from urllib.parse import urlsplit

# NFC Forum URI record identifier codes for the schemes a location page can have.
URI_PREFIX = {'https': 0x04, 'http': 0x03}


def _record(type_char: str, payload: bytes, first: bool, last: bool) -> bytes:
    """One short, well-known-type NDEF record."""
    if len(payload) > 255:
        raise ValueError('record payload over 255 bytes; shorten the host name')
    flags = 0x11 | (0x80 if first else 0) | (0x40 if last else 0)  # SR, TNF well-known
    return bytes([flags, 1, len(payload)]) + type_char.encode() + payload


def location_text(pk: int) -> str:
    """InvenTree's short barcode for a stock location."""
    return f'INV-SL{pk}'


def location_uri(base_url: str, pk: int) -> str:
    """The location's page, from the server's configured base URL."""
    return f'{base_url.rstrip("/")}/web/stock/location/{pk}'


def build_message(base_url: str, pk: int) -> bytes:
    """The NDEF message for a stock location's tag."""
    parts = urlsplit(base_url)
    scheme = parts.scheme or 'https'
    if scheme not in URI_PREFIX:
        raise ValueError(f'unsupported scheme {scheme!r} in base URL')
    host_and_path = parts.netloc + parts.path.rstrip('/')
    uri = bytes([URI_PREFIX[scheme]]) + f'{host_and_path}/web/stock/location/{pk}'.encode()
    text = b'\x02en' + location_text(pk).encode()  # UTF-8, language "en"
    return _record('U', uri, True, False) + _record('T', text, False, True)
