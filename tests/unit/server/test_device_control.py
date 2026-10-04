"""DeviceCommandService 写入 + 回读闭环单元测试。"""

from unittest.mock import AsyncMock

from wind_hub_core.model.command import CommandResult
from wind_hub_core.model.point import PointValue
from wind_hub_server.application.device.command import DeviceCommandService
from wind_hub_server.infra.point_store import InMemoryTrendStore


async def test_successful_command_reads_back_and_updates_stores() -> None:
    commander = AsyncMock()
    commander.write.return_value = CommandResult(command_id="c1", success=True)
    commander.read_point.return_value = PointValue(
        device_id="d1", point_id="setpoint", value=42.0
    )
    trend = InMemoryTrendStore()
    service = DeviceCommandService(commander, trend)

    result = await service.send(
        "d1", "setpoint", 42.0, command_id="c1"
    )

    assert result.success is True
    assert result.readback == 42.0
    assert trend.query("d1", {"setpoint"})["setpoint"][0].value == 42.0


async def test_failed_command_does_not_attempt_readback() -> None:
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c2", success=False, error="write rejected"
    )
    service = DeviceCommandService(commander, InMemoryTrendStore())

    result = await service.send("d1", "setpoint", 10.0, command_id="c2")

    assert result.success is False
    assert result.readback is None
    commander.read_point.assert_not_awaited()


async def test_readback_failure_does_not_fail_successful_write() -> None:
    """回读异常只记入 readback_error，不推翻已成功的写入。"""
    commander = AsyncMock()
    commander.write.return_value = CommandResult(command_id="c3", success=True)
    commander.read_point.side_effect = TimeoutError("readback timeout")
    service = DeviceCommandService(commander, InMemoryTrendStore())

    result = await service.send("d1", "setpoint", 10.0, command_id="c3")

    assert result.success is True
    assert result.readback is None
    assert result.readback_error == "readback timeout"
