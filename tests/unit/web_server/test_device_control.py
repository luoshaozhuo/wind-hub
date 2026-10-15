"""DeviceCommandService 写入 + 回读闭环单元测试。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from core.application.command import CommandResult
from core.application.protocol_contract import WriteOutcome
from core.domain.sample_value import Quality
from web_server.application.device.command import DeviceCommandService
from web_server.application.device.sample import PointValue
from web_server.infra.point_store import InMemoryTrendStore


def _point_value(value: float) -> PointValue:
    return PointValue(
        device_id="d1",
        point_id="setpoint",
        value=value,
        quality=Quality.GOOD,
        timestamp=datetime.now(UTC),
    )


async def test_successful_command_reads_back_and_updates_stores() -> None:
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c1", outcome=WriteOutcome.CONFIRMED_SUCCESS
    )
    commander.read_point.return_value = _point_value(42.0)
    trend = InMemoryTrendStore()
    service = DeviceCommandService(commander, trend)

    result = await service.send("d1", "setpoint", 42.0, command_id="c1")

    assert result.success is True
    assert result.readback == 42.0
    assert trend.query("d1", {"setpoint"})["setpoint"][0].value == 42.0


async def test_failed_command_does_not_attempt_readback() -> None:
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c2",
        outcome=WriteOutcome.CONFIRMED_FAILURE,
        error="write rejected",
    )
    service = DeviceCommandService(commander, InMemoryTrendStore())

    result = await service.send("d1", "setpoint", 10.0, command_id="c2")

    assert result.success is False
    assert result.readback is None
    commander.read_point.assert_not_awaited()


async def test_unknown_outcome_does_not_attempt_readback() -> None:
    """执行结果未知时按未确认处理：不回读、不声称成功，交由调用方人工核对。"""
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c4",
        outcome=WriteOutcome.UNKNOWN,
        error="response lost after write",
    )
    service = DeviceCommandService(commander, InMemoryTrendStore())

    result = await service.send("d1", "setpoint", 10.0, command_id="c4")

    assert result.success is False
    assert result.outcome is WriteOutcome.UNKNOWN
    assert result.readback is None
    commander.read_point.assert_not_awaited()


async def test_readback_failure_does_not_fail_successful_write() -> None:
    """回读异常只记入 readback_error，不推翻已成功的写入。"""
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c3", outcome=WriteOutcome.CONFIRMED_SUCCESS
    )
    commander.read_point.side_effect = TimeoutError("readback timeout")
    service = DeviceCommandService(commander, InMemoryTrendStore())

    result = await service.send("d1", "setpoint", 10.0, command_id="c3")

    assert result.success is True
    assert result.readback is None
    assert result.readback_error == "readback timeout"
