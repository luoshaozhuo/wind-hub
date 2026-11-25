"""Lossless two-bit quality encoding for device-level TDengine wide rows.

Each field occupies two bits in physical column order, least-significant bits first.
The caller must supply the complete physical point set, including absent values.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import IntEnum


class PointQuality(IntEnum):
    NOT_COLLECTED = 0
    GOOD = 1
    BAD = 2
    UNCERTAIN = 3


def pack_quality_bitmap(qualities: Sequence[int]) -> bytes:
    """Encode exactly one quality per physical value column."""
    bitmap = bytearray((len(qualities) + 3) // 4)
    for index, quality in enumerate(qualities):
        if type(quality) is not int or not 0 <= quality <= 3:
            raise ValueError(f"invalid quality at column {index}")
        bitmap[index // 4] |= quality << (2 * (index % 4))
    return bytes(bitmap)


def unpack_quality_bitmap(bitmap: bytes, point_count: int) -> tuple[PointQuality, ...]:
    """Decode a bitmap and reject noncanonical trailing padding."""
    if type(point_count) is not int or point_count < 0:
        raise ValueError("point_count must be a nonnegative integer")
    if len(bitmap) != (point_count + 3) // 4:
        raise ValueError("quality bitmap length does not match physical schema")
    if point_count % 4 and bitmap:
        used_bits = 2 * (point_count % 4)
        if bitmap[-1] >> used_bits:
            raise ValueError("quality bitmap has nonzero padding bits")
    return tuple(
        PointQuality((bitmap[index // 4] >> (2 * (index % 4))) & 0b11)
        for index in range(point_count)
    )
