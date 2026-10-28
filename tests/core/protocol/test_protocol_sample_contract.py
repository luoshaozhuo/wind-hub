"""ProtocolSample 时间戳契约：必填、UTC 时区、device/local 来源语义。"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from hat.drivers.iec101 import common as iec101

from core.application import Quality
from core.application.protocol_contract import ProtocolSample
from core.infrastructure.protocol.iec104.codec import decode_sample


def _cp56(
    year: int,
    month: int,
    day: int,
    hour: int,
    minute: int,
    *,
    summer: bool | None = False,
) -> iec101.Time:
    return iec101.Time(
        size=iec101.TimeSize.SEVEN,
        milliseconds=0,
        invalid=False,
        substituted=None,
        minutes=minute,
        summer_time=summer,
        hours=hour,
        day_of_week=None,
        day_of_month=day,
        months=month,
        years=year,
    )


def _floating_msg(time: object | None = None) -> iec101.DataMsg:
    return iec101.DataMsg(
        is_test=False,
        originator_address=1,
        asdu_address=1,
        io_address=100,
        data=iec101.FloatingData(
            value=iec101.FloatingValue(42.0),
            quality=iec101.MeasurementQuality(False, False, False, False, False),
        ),
        time=time,
        cause=iec101.DataResCause.SPONTANEOUS,
    )


class TestProtocolSampleValidation:
    def test_timestamp_is_required(self) -> None:
        with pytest.raises(TypeError):
            ProtocolSample(  # type: ignore[call-arg]
                point_id="p",
                value=1.0,
                quality=Quality.GOOD,
            )

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            ProtocolSample(
                point_id="p",
                value=1.0,
                timestamp=datetime(2026, 1, 1),  # naive
            )

    def test_aware_datetime_accepted_with_default_local_source(self) -> None:
        sample = ProtocolSample(
            point_id="p",
            value=1.0,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        )
        assert sample.timestamp_source == "local"


class TestIEC104NativeTimestamp:
    def test_native_cp56_timestamp_marked_as_device(self) -> None:
        # CP56 年仅两位：26 必须按世纪翻转解释为 2026，而非 1926。
        cp56 = _cp56(26, 1, 1, 12, 0)
        device_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

        sample = decode_sample("p", _floating_msg(cp56), device_tz=ZoneInfo("UTC"))

        assert sample.timestamp == device_time
        assert sample.timestamp.tzinfo is not None
        assert sample.timestamp_source == "device"

    def test_naive_device_wall_clock_uses_configured_device_timezone(self) -> None:
        """CP56Time2a 不带时区：墙钟时刻按显式设备时区解释，不依赖主机时区。"""
        # 设备本地墙钟 2026-01-01 12:00（上海冬令时）= 04:00 UTC。
        cp56 = _cp56(26, 1, 1, 12, 0)

        sample = decode_sample(
            "p",
            _floating_msg(cp56),
            device_tz=ZoneInfo("Asia/Shanghai"),
        )

        assert sample.timestamp == datetime(2026, 1, 1, 4, 0, 0, tzinfo=UTC)
        assert sample.timestamp.tzinfo is not None
        assert sample.timestamp_source == "device"

    def test_missing_timestamp_falls_back_to_local_receive_time(self) -> None:
        before = datetime.now(UTC)
        sample = decode_sample("p", _floating_msg(None))
        after = datetime.now(UTC)

        assert before <= sample.timestamp <= after
        assert sample.timestamp_source == "local"

    def test_invalid_timestamp_falls_back_to_local_receive_time(self) -> None:
        wall_clock = datetime(2026, 1, 1, 12, 0, 0)
        invalid_cp56 = iec101.time_from_datetime(wall_clock, invalid=True)

        before = datetime.now(UTC)
        sample = decode_sample("p", _floating_msg(invalid_cp56))
        after = datetime.now(UTC)

        assert before <= sample.timestamp <= after
        assert sample.timestamp_source == "local"
