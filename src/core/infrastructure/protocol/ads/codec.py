"""ADS 标量解码与连续通知区块编译。"""

from __future__ import annotations

import struct
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import pyads  # type: ignore[import-untyped]

from core.application.errors import ConfigError, ProtocolError
from core.application.protocol_contract import PointScalar, ProtocolSample, Quality

from .mapping import ADSPoint

_ADSERR_SYMBOL_NOT_FOUND = 1808

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


def compile_decoder(point: ADSPoint) -> Callable[[bytes | memoryview, int], PointScalar]:
    """一次编译标量解码器，运行时不重复查找类型或建立 ctypes 对象。"""
    if point.data_type == "STRING":
        size = point.size

        def decode_string(raw: bytes | memoryview, offset: int) -> str:
            return bytes(raw[offset : offset + size]).split(b"\x00", 1)[0].decode("utf-8")

        return decode_string
    unpacker = struct.Struct("<" + _FORMATS[point.data_type])
    if unpacker.size != point.size:
        raise ValueError(f"ADS point '{point.point_id}' size mismatch")
    return lambda raw, offset: unpacker.unpack_from(raw, offset)[0]


@dataclass(frozen=True, slots=True)
class NotificationBlock:
    """一组连续地址及预编译的数据布局。"""

    group: int
    offset: int
    size: int
    fields: tuple[tuple[ADSPoint, int, Callable[[bytes | memoryview, int], PointScalar]], ...]


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
        last = current[-1]
        end = last.offset + last.size
        fields = tuple(
            (point, point.offset - first.offset, compile_decoder(point)) for point in current
        )
        blocks.append(NotificationBlock(first.group, first.offset, end - first.offset, fields))

    previous: ADSPoint | None = None
    for point in ordered:
        if (
            previous is not None
            and point.group == previous.group
            and point.offset < previous.offset + previous.size
        ):
            raise ConfigError(f"ADS points '{previous.point_id}' and '{point.point_id}' overlap")
        previous = point
        if point.size > max_bytes:
            raise ValueError(f"ADS point '{point.point_id}' exceeds notification block size")
        if current:
            first = current[0]
            if point.group != first.group or point.offset + point.size - first.offset > max_bytes:
                append_block()
                current = []
        current.append(point)

    if current:
        append_block()
    return tuple(blocks)
def _plc_datatype(ads_name: str) -> Any:
    datatype = getattr(pyads, f"PLCTYPE_{ads_name}", None)
    if datatype is None:
        raise ConfigError(f"pyads does not expose PLCTYPE_{ads_name}")
    return datatype


def _decode_sum_write_errors(raw: object, count: int) -> tuple[int, ...]:
    """从 Sum Write 响应解出逐点 ADS 错误码。

    pyads 对无读取类型的 Sum Write 实际返回 ctypes ``c_ubyte`` 数组
    （并非 bytes，且 ctypes.Array 不注册为 collections.abc.Sequence），
    因此按结构化序列能力校验。
    """
    if (
        raw is None
        or isinstance(raw, str)
        or not (hasattr(raw, "__len__") and hasattr(raw, "__getitem__"))
    ):
        raise ProtocolError("ADS sum write returned an invalid response size")
    values = cast("Sequence[int]", raw)
    if len(values) != count * 4:
        raise ProtocolError("ADS sum write returned an invalid response size")
    return tuple(error for (error,) in struct.iter_unpack("<I", bytes(values)))


def _encode_value(value: float | int | bool | str, point: ADSPoint) -> bytes:
    """把单个 ADS 标量序列化为寻址宽度恰好一致的字节串。"""
    if point.data_type == "STRING":
        encoded = value.encode("utf-8") if isinstance(value, str) else b""
        if point.size <= 0 or len(encoded) >= point.size:
            raise ValueError(f"ADS STRING '{point.point_id}' exceeds configured size")
        return encoded.ljust(point.size, b"\x00")
    datatype = _plc_datatype(point.data_type)
    raw = bytes(datatype(value))
    if len(raw) != point.size:
        raise ValueError(f"ADS point '{point.point_id}' data width mismatch")
    return raw


def _as_point_scalar(
    value: object,
) -> float | int | bool | str | None:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"unsupported ADS value type '{type(value).__name__}'")


def _bad_sample(point_id: str) -> ProtocolSample:
    return ProtocolSample(
        point_id=point_id,
        value=None,
        timestamp=(received_at := datetime.now(UTC)),
        receive_time=received_at,
        quality=Quality.BAD,
    )


def _is_point_level_error(exc: BaseException) -> bool:
    ads_error = getattr(pyads, "ADSError", None)
    return (
        ads_error is not None
        and isinstance(exc, ads_error)
        and getattr(exc, "err_code", None) == _ADSERR_SYMBOL_NOT_FOUND
    )


_ADS_INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "SINT": (-128, 127),
    "USINT": (0, 255),
    "INT": (-32768, 32767),
    "UINT": (0, 65535),
    "DINT": (-2147483648, 2147483647),
    "UDINT": (0, 4294967295),
    "LINT": (-9223372036854775808, 9223372036854775807),
    "ULINT": (0, 18446744073709551615),
}


def _coerce_write_value(
    value: object,
    ads_type: str,
) -> float | int | bool | str:
    """把共享标量严格收敛到 ADS 基础类型。"""
    if ads_type == "BOOL":
        if type(value) is not bool:
            raise TypeError("ADS BOOL requires bool value")
        return value

    if ads_type == "STRING":
        if not isinstance(value, str):
            raise TypeError("ADS STRING requires string value")
        return value

    integer_range = _ADS_INTEGER_RANGES.get(ads_type)
    if integer_range is not None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"ADS {ads_type} requires integer-compatible value")
        integer = int(value)
        if float(value) != float(integer):
            raise ValueError(f"ADS {ads_type} requires an integer value")
        lower, upper = integer_range
        if not lower <= integer <= upper:
            raise ValueError(f"ADS {ads_type} value {integer} outside range {lower}..{upper}")
        return integer

    if ads_type in {"REAL", "LREAL"}:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"ADS {ads_type} requires numeric value")
        return float(value)

    raise ValueError(f"unsupported ADS type '{ads_type}'")



