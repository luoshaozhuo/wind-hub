"""ProtocolSample 时间戳契约：必填、UTC 时区、device/local 来源语义。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from core.application import Quality
from core.application.protocol_contract import ProtocolSample
from core.infrastructure.protocol.ads.subscription import _normalize_timestamp
from core.infrastructure.protocol.iec104.codec import sample_from_c104


class _C104Quality:
    value = 0


class _C104Point:
    def __init__(self, value: object, recorded_at: object = None) -> None:
        self.value = value
        self.quality = _C104Quality()
        self.recorded_at = recorded_at


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
        sample = sample_from_c104(_C104Point(42.0, device_time), "p")

        assert sample.timestamp == device_time
        assert sample.timestamp_source == "device"

    def test_naive_native_timestamp_falls_back_to_local(self) -> None:
        """CP56Time2a 不编码时区，naive 设备时间不得假定为 UTC，回退本地接收时刻。"""
        before = datetime.now(UTC)
        sample = sample_from_c104(_C104Point(42.0, datetime(2026, 1, 1, 12, 0, 0)), "p")
        after = datetime.now(UTC)

        assert before <= sample.timestamp <= after
        assert sample.timestamp_source == "local"

    def test_aware_native_timestamp_converted_to_utc(self) -> None:
        tz = timezone(timedelta(hours=8))
        sample = sample_from_c104(_C104Point(1, datetime(2026, 1, 1, 20, tzinfo=tz)), "p")

        assert sample.timestamp == datetime(2026, 1, 1, 12, tzinfo=UTC)
        assert sample.timestamp_source == "device"

    def test_missing_native_timestamp_falls_back_to_local_receive_time(self) -> None:
        before = datetime.now(UTC)
        sample = sample_from_c104(_C104Point(42.0, None), "p")
        after = datetime.now(UTC)

        assert before <= sample.timestamp <= after
        assert sample.timestamp_source == "local"

    def test_invalid_recorded_at_type_raises(self) -> None:
        with pytest.raises(TypeError, match="recorded_at"):
            sample_from_c104(_C104Point(42.0, "not-a-datetime"), "p")


class TestADSNotificationTimestamp:
    def test_missing_timestamp_falls_back_to_local(self) -> None:
        ts, source = _normalize_timestamp(None)

        assert ts.tzinfo is not None
        assert source == "local"

    def test_native_timestamp_marked_as_device(self) -> None:
        native = datetime(2026, 1, 1, tzinfo=UTC)
        ts, source = _normalize_timestamp(native)

        assert ts == native
        assert source == "device"
