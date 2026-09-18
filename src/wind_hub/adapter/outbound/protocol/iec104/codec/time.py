"""IEC 60870-5-104 CP56Time2a codec — 7-byte timestamp."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime

from wind_hub.domain.model.errors import ProtocolError


@dataclass(frozen=True)
class CP56Time2a:
    """IEC 60870-5-104 seven-octet binary time.

    Byte layout of CP56Time2a::

        byte 0-1:  milliseconds (0–59999, little-endian)
        byte 2:    minutes (bits 0–5), bit 7 = IV (invalid)
        byte 3:    hours (bits 0–4), bit 7 = SU (summer time)
        byte 4:    day-of-month (bits 0–4), day-of-week (bits 5–7, 1=Monday)
        byte 5:    month (bits 0–3)
        byte 6:    year (bits 0–6, 0-99 → 2000–2099)

    All timestamps in this project are interpreted as **UTC**.
    """

    milliseconds: int
    """Milliseconds (0–59999)."""

    minutes: int
    """Minutes (0–59)."""

    hours: int
    """Hours (0–23)."""

    day: int
    """Day of month (1–31)."""

    month: int
    """Month (1–12)."""

    year: int
    """Year (2000–2099).  Stored on wire as 0–99."""

    invalid: bool = False
    """IV bit — invalid timestamp."""

    summer_time: bool = False
    """SU bit — summer/daylight-saving time indicator."""


def encode_cp56time2a(ts: CP56Time2a) -> bytes:
    """Encode a CP56Time2a to 7 bytes.

    Raises:
        ProtocolError: On out-of-range field values.
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
    # day-of-week: 1=Monday; compute from date (optional — 0 if unknown)
    try:
        dow = datetime(ts.year, ts.month, ts.day, tzinfo=UTC).isoweekday()
    except ValueError:
        dow = 0
    day_byte |= (dow & 0x07) << 5  # bits 5-7
    month_byte = ts.month & 0x0F  # bits 0-3
    year_byte = (ts.year - 2000) & 0x7F  # bits 0-6

    return ms + bytes([minute_byte, hour_byte, day_byte, month_byte, year_byte])


def decode_cp56time2a(data: bytes, offset: int = 0) -> tuple[CP56Time2a, int]:
    """Decode a CP56Time2a from *data* at *offset*.

    Returns:
        ``(CP56Time2a, new_offset)`` tuple.

    Raises:
        ProtocolError: If fewer than 7 bytes remain.
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
    """Convert a Python ``datetime`` (must be tz-aware UTC) to ``CP56Time2a``.

    The sub-second part is converted to milliseconds (0–59999).
    """
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)

    total_ms = int(dt.microsecond / 1000) + (dt.second * 1000)
    # Clamp to 59999
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
    """Convert a ``CP56Time2a`` to a Python ``datetime`` (UTC).

    The ``milliseconds`` field is split into seconds and microseconds.
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
