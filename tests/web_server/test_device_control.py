"""DeviceCommandService 写入 + 回读闭环单元测试。"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from core.application.command import CommandResult
from core.application.protocol_contract import WriteOutcome
from core.domain.sample_value import Quality
from web_server.application.device.command import DeviceCommandService
from web_server.application.device.sample import PointValue
from web_server.infra.point_store import InMemoryTrendStore

pytestmark = pytest.mark.unit


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


async def test_same_command_id_returns_cached_result_without_second_write() -> None:
    """相同 command_id + 相同内容的重复请求返回首次结果，不发生二次写入。"""
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c9",
        outcome=WriteOutcome.UNKNOWN,
        error="timeout",
    )
    service = DeviceCommandService(commander, InMemoryTrendStore())

    first = await service.send("d1", "setpoint", 1.0, command_id="c9")
    second = await service.send("d1", "setpoint", 1.0, command_id="c9")

    assert second is first
    assert commander.write.await_count == 1


async def test_same_command_id_with_different_content_conflicts() -> None:
    """相同 command_id 携带不同写入内容必须拒绝，防止语义混淆。"""
    import pytest

    from web_server.application.device.command import CommandIdConflictError

    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c10",
        outcome=WriteOutcome.CONFIRMED_FAILURE,
    )
    service = DeviceCommandService(commander, InMemoryTrendStore())

    await service.send("d1", "setpoint", 1.0, command_id="c10")
    with pytest.raises(CommandIdConflictError):
        await service.send("d1", "setpoint", 2.0, command_id="c10")
    assert commander.write.await_count == 1


async def test_concurrent_duplicate_shares_single_execution() -> None:
    """并发重复请求共享同一次执行——in-flight 去重，绝不并行双写。"""
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_write(command):  # noqa: ANN001, ANN202
        started.set()
        await release.wait()
        return CommandResult(command_id=command.command_id, outcome=WriteOutcome.UNKNOWN)

    commander = AsyncMock()
    commander.write.side_effect = slow_write
    service = DeviceCommandService(commander, InMemoryTrendStore())

    first = asyncio.create_task(service.send("d1", "setpoint", 1.0, command_id="c11"))
    await started.wait()
    second = asyncio.create_task(service.send("d1", "setpoint", 1.0, command_id="c11"))
    await asyncio.sleep(0)
    release.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result is second_result
    assert commander.write.await_count == 1
