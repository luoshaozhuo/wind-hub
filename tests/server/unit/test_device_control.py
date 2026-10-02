"""DeviceControlUseCase 写入 + 回读闭环单元测试。"""

from unittest.mock import AsyncMock

from wind_hub_server.application.usecase.device_control import DeviceControlUseCase
from wind_hub_collector.domain.model.command import CommandResult
from wind_hub_collector.domain.model.point import PointValue
from wind_hub_server.infra.point_store import InMemoryLatestPointStore, InMemoryTrendStore


async def test_successful_command_reads_back_and_updates_stores() -> None:
    command = AsyncMock()
    command.send.return_value = CommandResult(command_id="c1", success=True)
    query = AsyncMock()
    query.read_point.return_value = PointValue(
        device_id="d1", point_id="setpoint", value=42.0
    )
    latest = InMemoryLatestPointStore()
    trend = InMemoryTrendStore()
    usecase = DeviceControlUseCase(command, query, latest, trend)

    result = await usecase.send(
        "d1", "setpoint", 42.0, command_id="c1"
    )

    assert result.success is True
    assert result.readback == 42.0
    assert latest.get("d1", "setpoint").value == 42.0
    assert trend.query("d1", {"setpoint"})["setpoint"][0].value == 42.0


async def test_failed_command_does_not_attempt_readback() -> None:
    command = AsyncMock()
    command.send.return_value = CommandResult(
        command_id="c2", success=False, error="write rejected"
    )
    query = AsyncMock()
    usecase = DeviceControlUseCase(
        command, query, InMemoryLatestPointStore(), InMemoryTrendStore()
    )

    result = await usecase.send("d1", "setpoint", 10.0, command_id="c2")

    assert result.success is False
    assert result.readback is None
    query.read_point.assert_not_awaited()
