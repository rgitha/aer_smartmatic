"""Minimal, dependency-free CBOR decoder for Smart Matic messages."""
from __future__ import annotations

import struct
from typing import Any

_BREAK = object()


class CBORError(ValueError):
    """The data is not valid CBOR."""


class CBORIncompleteError(CBORError):
    """Valid but truncated data: more fragments may follow."""


def loads(data: bytes) -> Any:
    """Decode exactly one CBOR item spanning all of ``data``."""
    value, end = decode(data)
    if end != len(data):
        raise CBORError(f"{len(data) - end} trailing byte(s) after CBOR item")
    return value


def decode(data: bytes | memoryview, offset: int = 0) -> tuple[Any, int]:
    """Decode one CBOR item at ``offset``; return (value, next offset)."""
    value, end = _item(memoryview(data), offset)
    if value is _BREAK:
        raise CBORError("unexpected break")
    return value, end


def _need(data: memoryview, offset: int, n: int) -> None:
    if offset + n > len(data):
        raise CBORIncompleteError("truncated CBOR data")


def _argument(data: memoryview, offset: int, info: int) -> tuple[int | None, int]:
    if info < 24:
        return info, offset
    if info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        _need(data, offset, size)
        return int.from_bytes(data[offset:offset + size], "big"), offset + size
    if info == 31:
        return None, offset
    raise CBORError(f"reserved additional info {info}")


def _item(data: memoryview, offset: int) -> tuple[Any, int]:
    _need(data, offset, 1)
    initial = data[offset]
    offset += 1
    major, info = initial >> 5, initial & 0x1F

    if major == 7:
        return _simple(data, offset, info)

    arg, offset = _argument(data, offset, info)

    if major == 0:
        if arg is None:
            raise CBORError("indefinite-length integer")
        return arg, offset
    if major == 1:
        if arg is None:
            raise CBORError("indefinite-length integer")
        return -1 - arg, offset
    if major in (2, 3):
        return _string(data, offset, major, arg)
    if major == 4:
        return _array(data, offset, arg)
    if major == 5:
        return _map(data, offset, arg)
    # Tag: return the tagged item.
    return decode(data, offset)


def _simple(data: memoryview, offset: int, info: int) -> tuple[Any, int]:
    if info == 20:
        return False, offset
    if info == 21:
        return True, offset
    if info in (22, 23):  # null, undefined
        return None, offset
    if info == 25:
        _need(data, offset, 2)
        return struct.unpack_from(">e", data, offset)[0], offset + 2
    if info == 26:
        _need(data, offset, 4)
        return struct.unpack_from(">f", data, offset)[0], offset + 4
    if info == 27:
        _need(data, offset, 8)
        return struct.unpack_from(">d", data, offset)[0], offset + 8
    if info == 31:
        return _BREAK, offset
    if info < 24:
        return info, offset
    if info == 24:
        _need(data, offset, 1)
        return data[offset], offset + 1
    raise CBORError(f"unsupported simple value {info}")


def _string(
    data: memoryview, offset: int, major: int, length: int | None
) -> tuple[Any, int]:
    if length is None:
        chunks = []
        while True:
            chunk, offset = _item(data, offset)
            if chunk is _BREAK:
                break
            chunks.append(chunk)
        joined = b"".join(c.encode() if isinstance(c, str) else c for c in chunks)
    else:
        _need(data, offset, length)
        joined = bytes(data[offset:offset + length])
        offset += length

    if major == 2:
        return joined, offset
    try:
        return joined.decode("utf-8"), offset
    except UnicodeDecodeError as e:
        raise CBORError(f"invalid UTF-8 in text string: {e}") from e


def _array(data: memoryview, offset: int, length: int | None) -> tuple[list[Any], int]:
    items: list[Any] = []
    if length is None:
        while True:
            value, offset = _item(data, offset)
            if value is _BREAK:
                return items, offset
            items.append(value)
    for _ in range(length):
        value, offset = decode(data, offset)
        items.append(value)
    return items, offset


def _map(data: memoryview, offset: int, length: int | None) -> tuple[dict[Any, Any], int]:
    result: dict[Any, Any] = {}
    count = 0
    while length is None or count < length:
        key, offset = _item(data, offset)
        if key is _BREAK:
            if length is None:
                return result, offset
            raise CBORError("unexpected break in definite-length map")
        value, offset = decode(data, offset)
        try:
            result[key] = value
        except TypeError as e:
            raise CBORError(f"unsupported map key {key!r}") from e
        count += 1
    return result, offset
