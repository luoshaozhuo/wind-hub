"""ProtocolSample 时间戳契约：必填、UTC 时区、device/local 来源语义。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hat.drivers.iec101 import common as iec101

from core.application import Quality
from core.application.protocol_contract import ProtocolSample
from core.infrastructure.protocol.iec104.codec import decode_sample


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
        device_time = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
        cp56 = iec101.time_from_datetime(device_time)

        sample = decode_sample("p", _floating_msg(cp56))

        assert sample.timestamp == device_time
        assert sample.timestamp.tzinfo is not None
        assert sample.timestamp_source == "device"

    def test_naive_device_wall_clock_is_local_wall_time(self) -> None:
        """CP56Time2a 不带时区：设备本地墙钟时刻由 hat 按主机本地时区归一。"""
        wall_clock = datetime(2026, 1, 1, 12, 0, 0)  # 设备本地墙钟，无时区
        cp56 = iec101.time_from_datetime(wall_clock)

        sample = decode_sample("p", _floating_msg(cp56))

        assert sample.timestamp == iec101.time_to_datetime(cp56)
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
