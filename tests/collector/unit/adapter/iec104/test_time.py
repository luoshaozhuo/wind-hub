"""Tests for CP56Time2a codec."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wind_hub.adapter.outbound.protocol.iec104.codec.time import (
    CP56Time2a,
    decode_cp56time2a,
    encode_cp56time2a,
    from_datetime,
    to_datetime,
)
from wind_hub.domain.model.errors import ProtocolError


class TestEncodeCP56Time2a:
    def test_known_sample(self) -> None:
        """Encode a known timestamp and verify field encoding."""
        ts = CP56Time2a(
            milliseconds=12345,
            minutes=30,
            hours=12,
            day=15,
            month=9,
            year=2026,
        )
        data = encode_cp56time2a(ts)
        assert len(data) == 7
        # ms=12345=0x3039 LE
        assert data[0] == 0x39
        assert data[1] == 0x30
        # min=30, no flags
        assert data[2] == 0x1E
        # hour=12
        assert data[3] == 0x0C
        # day=15, DOW computed (bits 5-7)
        assert (data[4] & 0x1F) == 0x0F  # day-of-month
        # month=9
        assert data[5] == 0x09
        # year=2026 → 26
        assert data[6] == 0x1A

    def test_zero_time(self) -> None:
        ts = CP56Time2a(
            milliseconds=0,
            minutes=0,
            hours=0,
            day=1,
            month=1,
            year=2000,
        )
        data = encode_cp56time2a(ts)
        assert len(data) == 7
        assert data[:2] == b"\x00\x00"  # ms
        assert data[2] == 0x00  # min
        assert data[3] == 0x00  # hour
        assert (data[4] & 0x1F) == 0x01  # day=1
        assert data[5] == 0x01  # month
        assert data[6] == 0x00  # year

    def test_invalid_flag(self) -> None:
        ts = CP56Time2a(
            milliseconds=0,
            minutes=0,
            hours=0,
            day=1,
            month=1,
            year=2000,
            invalid=True,
        )
        data = encode_cp56time2a(ts)
        # minute byte should have bit 7 set
        assert data[2] & 0x80 == 0x80

    def test_summer_time_flag(self) -> None:
        ts = CP56Time2a(
            milliseconds=0,
            minutes=0,
            hours=0,
            day=1,
            month=1,
            year=2000,
            summer_time=True,
        )
        data = encode_cp56time2a(ts)
        # hour byte should have bit 7 set
        assert data[3] & 0x80 == 0x80

    def test_max_milliseconds(self) -> None:
        ts = CP56Time2a(
            milliseconds=59999,
            minutes=0,
            hours=0,
            day=1,
            month=1,
            year=2000,
        )
        data = encode_cp56time2a(ts)
        result = decode_cp56time2a(data)[0]
        assert result.milliseconds == 59999

    @pytest.mark.parametrize(
        "field,value",
        [("milliseconds", 60000), ("minutes", 60), ("hours", 24)],
    )
    def test_out_of_range_field_raises(self, field: str, value: int) -> None:
        kwargs = {
            "milliseconds": 0,
            "minutes": 0,
            "hours": 0,
            "day": 1,
            "month": 1,
            "year": 2000,
        }
        kwargs[field] = value
        ts = CP56Time2a(**kwargs)
        with pytest.raises(ProtocolError, match="out of range"):
            encode_cp56time2a(ts)


class TestDecodeCP56Time2a:
    def test_known_sample(self) -> None:
        data = bytes([0x39, 0x30, 0x1E, 0x0C, 0x6F, 0x09, 0x1A])
        ts, off = decode_cp56time2a(data)
        assert off == 7
        assert ts.milliseconds == 12345
        assert ts.minutes == 30
        assert ts.hours == 12
        assert ts.day == 15
        assert ts.month == 9
        assert ts.year == 2026
        assert ts.invalid is False
        assert ts.summer_time is False

    def test_invalid_flag(self) -> None:
        data = bytes([0x00, 0x00, 0x80, 0x00, 0xC1, 0x01, 0x00])
        ts, _off = decode_cp56time2a(data)
        assert ts.invalid is True

    def test_summer_time_flag(self) -> None:
        data = bytes([0x00, 0x00, 0x00, 0x80, 0xC1, 0x01, 0x00])
        ts, _off = decode_cp56time2a(data)
        assert ts.summer_time is True

    def test_with_offset(self) -> None:
        data = b"\xaa\xaa" + bytes([0x00, 0x00, 0x00, 0x00, 0xC1, 0x01, 0x00])
        ts, off = decode_cp56time2a(data, offset=2)
        assert off == 9
        assert ts.day == 1

    def test_insufficient_data(self) -> None:
        with pytest.raises(ProtocolError, match="need 7 bytes"):
            decode_cp56time2a(b"\x00\x01\x02\x03\x04\x05")


class TestCP56Time2aRoundtrip:
    def test_roundtrip_normal(self) -> None:
        ts = CP56Time2a(
            milliseconds=12345,
            minutes=30,
            hours=12,
            day=15,
            month=9,
            year=2026,
        )
        result = decode_cp56time2a(encode_cp56time2a(ts))[0]
        assert result == ts

    def test_roundtrip_leap_year(self) -> None:
        ts = CP56Time2a(
            milliseconds=0,
            minutes=0,
            hours=0,
            day=29,
            month=2,
            year=2024,
        )
        result = decode_cp56time2a(encode_cp56time2a(ts))[0]
        assert result == ts

    def test_roundtrip_with_flags(self) -> None:
        ts = CP56Time2a(
            milliseconds=59999,
            minutes=59,
            hours=23,
            day=31,
            month=12,
            year=2099,
            invalid=True,
            summer_time=True,
        )
        result = decode_cp56time2a(encode_cp56time2a(ts))[0]
        assert result == ts


class TestDatetimeConversion:
    def test_from_datetime(self) -> None:
        dt = datetime(2026, 9, 15, 12, 30, 0, tzinfo=UTC)
        ts = from_datetime(dt)
        assert ts.year == 2026
        assert ts.month == 9
        assert ts.day == 15
        assert ts.hours == 12
        assert ts.minutes == 30

    def test_from_datetime_with_milliseconds(self) -> None:
        dt = datetime(2026, 9, 15, 12, 30, 12, 345000, tzinfo=UTC)
        ts = from_datetime(dt)
        assert ts.milliseconds == 12345  # 12 * 1000 + 345

    def test_to_datetime(self) -> None:
        ts = CP56Time2a(
            milliseconds=12345,
            minutes=30,
            hours=12,
            day=15,
            month=9,
            year=2026,
        )
        dt = to_datetime(ts)
        assert dt.year == 2026
        assert dt.month == 9
        assert dt.day == 15
        assert dt.hour == 12
        assert dt.minute == 30
        assert dt.second == 12
        assert dt.microsecond == 345000
        assert dt.tzinfo == UTC

    def test_roundtrip_datetime(self) -> None:
        dt = datetime(2026, 9, 15, 12, 30, 12, 345000, tzinfo=UTC)
        ts = from_datetime(dt)
        result = to_datetime(ts)
        assert result == dt
