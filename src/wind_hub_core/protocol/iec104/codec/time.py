"""IEC 60870-5-104 CP56Time2a 7 字节时标 codec。"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime

from wind_hub_core.model.errors import ProtocolError


@dataclass(frozen=True)
class CP56Time2a:
    """IEC104 七字节时标。

    本项目统一按 UTC 解释和生成 CP56Time2a，不在 codec 内执行本地时区转换。
    """

    milliseconds: int
    """毫秒计数，范围 0~59999，包含秒。"""

    minutes: int
    """分钟，范围 0~59。"""

    hours: int
    """小时，范围 0~23。"""

    day: int
    """月内日，范围 1~31。"""

    month: int
    """月份，范围 1~12。"""

    year: int
    """年份 2000~2099，wire 上保存为 0~99。"""

    invalid: bool = False
    """IV 位：时标无效。"""

    summer_time: bool = False
    """SU 位：夏令时标志。"""


def encode_cp56time2a(ts: CP56Time2a) -> bytes:
    """把 CP56Time2a 编码为 7 字节。

    Args:
        ts: 已拆分字段的 CP56Time2a。

    Returns:
        7 字节 wire 编码。

    Raises:
        ProtocolError: 任一字段越界。
    """
    if not (0 <= ts.milliseconds <= 59999):
        raise ProtocolError(f"milliseconds {ts.milliseconds} out of range [0, 59999]")
    if not (0 <= ts.minutes <= 59):
        raise ProtocolError(f"minutes {ts.minutes} out of range [0, 59]")
    if not (0 <= ts.hours <= 23):
        raise ProtocolError(f"hours {ts.hours} out of range [0, 23]")
    if not (1 <= ts.day <= 31):
        raise ProtocolError(f"day {ts.day} out of range [1, 31]")
    if not (1 <= ts.month <= 12):
        raise ProtocolError(f"month {ts.month} out of range [1, 12]")
    if not (2000 <= ts.year <= 2099):
        raise ProtocolError(f"year {ts.year} out of range [2000, 2099]")

    ms = struct.pack("<H", ts.milliseconds)  # bytes 0-1
    minute_byte = ts.minutes & 0x3F  # bits 0-5
    if ts.invalid:
        minute_byte |= 0x80  # bit 7 = IV
    hour_byte = ts.hours & 0x1F  # bits 0-4
    if ts.summer_time:
        hour_byte |= 0x80  # bit 7 = SU
    day_byte = ts.day & 0x1F  # bits 0-4
    # 星期编码 1=Monday；日期无法构造时写 0。
    try:
        dow = datetime(ts.year, ts.month, ts.day, tzinfo=UTC).isoweekday()
    except ValueError:
        dow = 0
    day_byte |= (dow & 0x07) << 5  # bits 5-7
    month_byte = ts.month & 0x0F  # bits 0-3
    year_byte = (ts.year - 2000) & 0x7F  # bits 0-6

    return ms + bytes([minute_byte, hour_byte, day_byte, month_byte, year_byte])


def decode_cp56time2a(data: bytes, offset: int = 0) -> tuple[CP56Time2a, int]:
    """从指定 offset 解码 CP56Time2a。

    Args:
        data: 原始字节缓冲区。
        offset: 时标起始偏移。

    Returns:
        (CP56Time2a, new_offset)。

    Raises:
        ProtocolError: 剩余字节不足 7 个。
    """
    if len(data) - offset < 7:
        raise ProtocolError(
            f"CP56Time2a decode: need 7 bytes at offset {offset}, " f"got {len(data) - offset}"
        )

    ms = struct.unpack_from("<H", data, offset)[0]
    minute_byte = data[offset + 2]
    hour_byte = data[offset + 3]
    day_byte = data[offset + 4]
    month_byte = data[offset + 5]
    year_byte = data[offset + 6]

    return CP56Time2a(
        milliseconds=ms,
        minutes=minute_byte & 0x3F,
        hours=hour_byte & 0x1F,
        day=day_byte & 0x1F,
        month=month_byte & 0x0F,
        year=2000 + (year_byte & 0x7F),
        invalid=bool(minute_byte & 0x80),
        summer_time=bool(hour_byte & 0x80),
    ), offset + 7


def from_datetime(dt: datetime) -> CP56Time2a:
    """把 datetime 转换为 UTC CP56Time2a。

    无时区 datetime 按 UTC 解释；有时区值先转换为 UTC。

    Args:
        dt: Python datetime。

    Returns:
        CP56Time2a。
    """
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

    total_ms = int(dt.microsecond / 1000) + (dt.second * 1000)
    # CP56Time2a 毫秒字段最大为 59999。
    total_ms = min(total_ms, 59999)

    return CP56Time2a(
        milliseconds=total_ms,
        minutes=dt.minute,
        hours=dt.hour,
        day=dt.day,
        month=dt.month,
        year=dt.year,
        invalid=False,
        summer_time=False,
    )


def to_datetime(ts: CP56Time2a) -> datetime:
    """把 CP56Time2a 转换为 UTC datetime。

    Args:
        ts: CP56Time2a。

    Returns:
        tzinfo=UTC 的 datetime。
    """
    sec = ts.milliseconds // 1000
    us = (ts.milliseconds % 1000) * 1000
    return datetime(
        ts.year,
        ts.month,
        ts.day,
        ts.hours,
        ts.minutes,
        sec,
        us,
        tzinfo=UTC,
    )
