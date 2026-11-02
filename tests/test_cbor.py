"""The CBOR decoder's less common items and its errors."""
import pytest

from godrej import cbor


@pytest.mark.parametrize(
    ("hexdata", "value"),
    [
        ("20", -1),
        ("3863", -100),
        ("f4", False),
        ("f5", True),
        ("f6", None),
        ("f7", None),
        ("f93c00", 1.0),
        ("fa3fc00000", 1.5),
        ("fb3ff8000000000000", 1.5),
        ("f0", 16),  # unassigned simple value
        ("f8ff", 255),
        ("c11a514b67b0", 1363896240),  # tagged: the item itself
        ("5f42010243030405ff", b"\x01\x02\x03\x04\x05"),  # chunked bytes
        ("7f657374726561646d696e67ff", "streaming"),
        ("9f0102ff", [1, 2]),
        ("820102", [1, 2]),
        ("a1616101", {"a": 1}),
    ],
)
def test_decodes(hexdata, value):
    assert cbor.loads(bytes.fromhex(hexdata)) == value


@pytest.mark.parametrize(
    "hexdata",
    [
        "1c",  # reserved additional info
        "1f",  # indefinite-length unsigned integer
        "3f",  # indefinite-length negative integer
        "f8",  # simple value missing its byte
        "f9",  # half float missing its bytes
        "fc",  # reserved simple value
        "62c328",  # invalid UTF-8
        "a201ff",  # break inside a definite-length map
        "a1820102f5",  # list as a map key
        "ff",  # a lone break
        "0102",  # trailing bytes
    ],
)
def test_rejects(hexdata):
    with pytest.raises(cbor.CBORError):
        cbor.loads(bytes.fromhex(hexdata))
