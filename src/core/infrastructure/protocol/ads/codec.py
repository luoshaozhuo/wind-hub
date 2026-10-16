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
    points: Sequence[ADSPoint], *, max_bytes: int = 1024
) -> tuple[NotificationBlock, ...]:
    """按 IndexGroup 和区块总跨度分组；空洞计入字节上限。"""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")

    ordered = sorted(points, key=lambda point: (point.group, point.offset))
    blocks: list[NotificationBlock] = []
    current: list[ADSPoint] = []

    def append_block() -> None:
        first = current[0]
        end = max(point.offset + point.size for point in current)
        fields = tuple(
            (point, point.offset - first.offset, compile_decoder(point))
            for point in current
        )
        blocks.append(NotificationBlock(first.group, first.offset, end - first.offset, fields))

    for point in ordered:
        if point.size > max_bytes:
            raise ValueError(f"ADS point '{point.point_id}' exceeds notification block size")
        if current:
            first = current[0]
            if (
                point.group != first.group
                or point.offset + point.size - first.offset > max_bytes
            ):
                append_block()
                current = []
        current.append(point)

    if current:
        append_block()
    return tuple(blocks)
