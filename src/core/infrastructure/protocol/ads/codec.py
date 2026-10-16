"""ADS 标量解码与连续通知区块编译。"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Callable

from core.application.protocol_contract import PointScalar

from .mapping import ADSPoint

_FORMATS = {
    "BOOL": "?",
    "SINT": "b",
    "USINT": "B",
    "INT": "h",
    "UINT": "H",
    "DINT": "i",
    "UDINT": "I",
    "LINT": "q",
    "ULINT": "Q",
    "REAL": "f",
    "LREAL": "d",
}

def compile_decoder(point: ADSPoint) -> Callable[[bytes | memoryview], PointScalar]:
    """一次编译标量解码器，运行时不重复查找类型或建立 ctypes 对象。"""
    if point.data_type == "STRING":
        return lambda raw: bytes(raw).split(b"\x00", 1)[0].decode("utf-8")
    unpacker = struct.Struct("<" + _FORMATS[point.data_type])
    if unpacker.size != point.size:
        raise ValueError(f"ADS point '{point.point_id}' size mismatch")
    return lambda raw: unpacker.unpack_from(raw)[0]


@dataclass(frozen=True, slots=True)
class NotificationBlock:
    """一组连续地址及预编译的数据布局。"""

    group: int
    offset: int
    size: int
    fields: tuple[tuple[ADSPoint, int, Callable[[bytes | memoryview], PointScalar]], ...]


def compile_notification_blocks(
    points: Sequence[ADSPoint], *, max_bytes: int = 1024, max_gap_bytes: int = 16
) -> tuple[NotificationBlock, ...]:
    """同组地址按允许的最大间隙与通知字节上限分块。"""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if max_gap_bytes < 0:
        raise ValueError("max_gap_bytes must be non-negative")
    ordered = sorted(points, key=lambda point: (point.group, point.offset))
    blocks: list[NotificationBlock] = []
    fields: list[tuple[ADSPoint, int, Callable[[bytes | memoryview], PointScalar]]] = []
    group = offset = size = 0

    def flush() -> None:
        if fields:
            blocks.append(NotificationBlock(group, offset, size, tuple(fields)))

    for point in ordered:
        if point.size > max_bytes:
            raise ValueError(f"ADS point '{point.point_id}' exceeds notification block size")
        gap = point.offset - (offset + size)
        if fields and (
            point.group != group
            or gap < 0
            or gap > max_gap_bytes
            or point.offset + point.size - offset > max_bytes
        ):
            flush()
            fields = []
        if not fields:
            group, offset, size = point.group, point.offset, 0
        fields.append((point, point.offset - offset, compile_decoder(point)))
        size = point.offset + point.size - offset
    flush()
    return tuple(blocks)
