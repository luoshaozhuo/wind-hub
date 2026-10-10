"""ProtocolSample 时间戳契约：必填、UTC 时区、device/local 来源语义。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from collector.application.config import RuntimeParams
from commander.application.config import CommanderConfig
from core.application import Quality
from core.application.protocol_contract import ProtocolSample
from core.application.recovery import RecoverySettings
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

    def test_naive_native_timestamp_interpreted_as_utc_device_time(self) -> None:
        naive = datetime(2026, 1, 1, 12, 0, 0)
        sample = sample_from_c104(_C104Point(42.0, naive), "p")

        assert sample.timestamp == naive.replace(tzinfo=UTC)
        assert sample.timestamp_source == "device"

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


class TestDriverReadTimestamps:
    """三协议读路径（GOOD/BAD/批量）样本均带必填 UTC 时间戳。"""

    @pytest.mark.asyncio
    async def test_modbus_read_one_good_and_bad_carry_local_timestamps(self) -> None:
        from tests.unit.shared_core.test_modbus_single_ops import (
            _Client,
            _driver,
            _point,
            _Response,
        )

        client = _Client()
        client.response = _Response(registers=[7])
        driver = _driver({"h": _point("h", "holding", 100)}, client)

        good = await driver.read_one("h")
        assert good.timestamp.tzinfo is not None
        assert good.timestamp_source == "local"

        client.response = _Response(registers=[])  # 解码失败 → BAD
        bad = await driver.read_one("h")
        assert bad.quality == Quality.BAD
        assert bad.timestamp.tzinfo is not None
        assert bad.timestamp_source == "local"

    @pytest.mark.asyncio
    async def test_modbus_read_many_shares_single_batch_timestamp(self) -> None:
        from tests.unit.shared_core.test_modbus_single_ops import (
            _Client,
            _driver,
            _point,
            _Response,
        )

        client = _Client()
        client.response = _Response(registers=[1, 2])
        driver = _driver(
            {"a": _point("a", "holding", 100), "b": _point("b", "holding", 101)},
            client,
        )

        samples = await driver.read_many(["a", "b"])
        assert all(s.timestamp.tzinfo is not None for s in samples)
        assert samples[0].timestamp == samples[1].timestamp
        assert all(s.timestamp_source == "local" for s in samples)

    @pytest.mark.asyncio
    async def test_iec104_mirror_miss_bad_sample_has_local_timestamp(self) -> None:
        from tests.unit.shared_core.test_iec104_adapter import (
            _driver_for_monitoring_point,
        )

        driver, point = _driver_for_monitoring_point()
        driver._is_open = True

        sample = await driver.read_one(point.point_id)
        assert sample.quality == Quality.BAD
        assert sample.timestamp.tzinfo is not None
        assert sample.timestamp_source == "local"


class TestRecoveryDefaults:
    def test_recovery_settings_default_timeouts_are_one_second(self) -> None:
        settings = RecoverySettings()

        assert settings.read_retries == 1
        assert settings.retry_interval == 1.0
        assert settings.connect_timeout == 1.0
        assert settings.read_timeout == 1.0
        assert settings.write_timeout == 1.0

    def test_collector_runtime_defaults_are_one_second(self) -> None:
        params = RuntimeParams()

        assert params.connect_timeout == 1.0
        assert params.read_timeout == 1.0
        assert params.write_timeout == 1.0
        assert params.read_retries == 1
        assert params.retry_interval == 1.0

    def test_commander_config_default_timeouts_are_one_second(self) -> None:
        config = CommanderConfig()

        assert config.connect_timeout == 1.0
        assert config.read_timeout == 1.0
        assert config.write_timeout == 1.0
        assert config.read_retries == 1
        assert config.retry_interval == 1.0


class TestRenameResidue:
    def test_no_recovery_port_symbol_remains(self) -> None:
        """§3：RecoveryPort 已重命名为 RecoveringProtocol，不留别名。"""
        import core.application.recovery as recovery_module

        assert not hasattr(recovery_module, "RecoveryPort")
        assert hasattr(recovery_module, "RecoveringProtocol")
