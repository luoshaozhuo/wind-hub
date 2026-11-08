"""DeviceCommandService 写入 + 回读闭环单元测试（v1 持久化 at-most-once）。

DurableCommandRegistry 使用真实 SQLite 落盘，覆盖跨「重启」的预留/终态
语义；Commander 与 Collector 读端口以 AsyncMock 替身，不发起真实网络。
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from core.application.protocol_contract import WriteOutcome
from core.domain.sample_value import Quality
from web_server.application.device.command import (
    CommandIdConflictError,
    DeviceCommandService,
)
from web_server.application.device.command_model import CommandResult
from web_server.application.device.durable_command import DurableCommandRegistry
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


def _service(
    tmp_path, commander: AsyncMock, reads: AsyncMock | None = None
) -> DeviceCommandService:
    journal = DurableCommandRegistry(tmp_path / "commands.sqlite3")
    if reads is None:
        reads = AsyncMock()
        reads.read_point.return_value = _point_value(1.0)
    return DeviceCommandService(commander, reads, InMemoryTrendStore(), journal)


async def test_successful_command_reads_back_and_updates_stores(tmp_path) -> None:
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c1", outcome=WriteOutcome.CONFIRMED_SUCCESS
    )
    reads = AsyncMock()
    reads.read_point.return_value = _point_value(42.0)
    trend = InMemoryTrendStore()
    service = DeviceCommandService(
        commander,
        reads,
        trend,
        DurableCommandRegistry(tmp_path / "commands.sqlite3"),
    )

    result = await service.send("d1", "setpoint", 42.0, command_id="c1")

    assert result.success is True
    assert result.readback == 42.0
    assert trend.query("d1", {"setpoint"})["setpoint"][0].value == 42.0


async def test_failed_command_does_not_attempt_readback(tmp_path) -> None:
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c2",
        outcome=WriteOutcome.CONFIRMED_FAILURE,
        error="write rejected",
    )
    reads = AsyncMock()
    service = _service(tmp_path, commander, reads)

    result = await service.send("d1", "setpoint", 10.0, command_id="c2")

    assert result.success is False
    assert result.readback is None
    reads.read_point.assert_not_awaited()


async def test_unknown_outcome_does_not_attempt_readback(tmp_path) -> None:
    """执行结果未知时按未确认处理：不回读、不声称成功，交由调用方人工核对。"""
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c4",
        outcome=WriteOutcome.UNKNOWN,
        error="response lost after write",
    )
    reads = AsyncMock()
    service = _service(tmp_path, commander, reads)

    result = await service.send("d1", "setpoint", 10.0, command_id="c4")

    assert result.success is False
    assert result.outcome is WriteOutcome.UNKNOWN
    assert result.readback is None
    reads.read_point.assert_not_awaited()


async def test_readback_failure_does_not_fail_successful_write(tmp_path) -> None:
    """回读异常只记入 readback_error，不推翻已成功的写入。"""
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c3", outcome=WriteOutcome.CONFIRMED_SUCCESS
    )
    reads = AsyncMock()
    reads.read_point.side_effect = TimeoutError("readback timeout")
    service = _service(tmp_path, commander, reads)

    result = await service.send("d1", "setpoint", 10.0, command_id="c3")

    assert result.success is True
    assert result.readback is None
    assert result.readback_error == "readback timeout"


async def test_same_command_id_returns_cached_result_without_second_write(tmp_path) -> None:
    """相同 command_id + 相同内容的重复请求返回持久化首结果，不发生二次写入。"""
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c9",
        outcome=WriteOutcome.CONFIRMED_SUCCESS,
    )
    service = _service(tmp_path, commander)

    first = await service.send("d1", "setpoint", 1.0, command_id="c9")
    second = await service.send("d1", "setpoint", 1.0, command_id="c9")

    assert first.command_id == second.command_id == "c9"
    assert second.outcome is WriteOutcome.CONFIRMED_SUCCESS
    commander.write.assert_awaited_once()


async def test_same_command_id_with_different_content_conflicts(tmp_path) -> None:
    """相同 command_id 携带不同写入内容必须拒绝，防止语义混淆。"""
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c10",
        outcome=WriteOutcome.CONFIRMED_FAILURE,
    )
    service = _service(tmp_path, commander)

    await service.send("d1", "setpoint", 1.0, command_id="c10")
    with pytest.raises(CommandIdConflictError):
        await service.send("d1", "setpoint", 2.0, command_id="c10")
    commander.write.assert_awaited_once()


async def test_concurrent_duplicate_shares_single_execution(tmp_path) -> None:
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
    service = _service(tmp_path, commander)

    first = asyncio.create_task(service.send("d1", "setpoint", 1.0, command_id="c11"))
    await started.wait()
    second = asyncio.create_task(service.send("d1", "setpoint", 1.0, command_id="c11"))
    await asyncio.sleep(0)
    release.set()

    first_result, second_result = await asyncio.gather(first, second)
    assert first_result is second_result
    commander.write.assert_awaited_once()


async def test_pending_record_after_crash_returns_unknown_without_resubmit(tmp_path) -> None:
    """首次提交后进程崩溃（预留停留在 pending）：新实例同 ID 请求只给 UNKNOWN，
    绝不重新提交现场控制指令。"""
    journal = DurableCommandRegistry(tmp_path / "commands.sqlite3")

    # 模拟崩溃现场：预留成功但 finish 从未发生。fingerprint 必须与
    # DeviceCommandService 内部计算一致，否则会走内容冲突路径而非 pending 路径。
    fingerprint = DeviceCommandService._fingerprint("d1", "setpoint", 1.0, 5.0)
    state, _ = await journal.reserve("c-crash", fingerprint)
    assert state == "new"

    commander = AsyncMock()
    service = DeviceCommandService(commander, AsyncMock(), InMemoryTrendStore(), journal)

    result = await service.send("d1", "setpoint", 1.0, command_id="c-crash")

    assert result.outcome is WriteOutcome.UNKNOWN
    assert result.success is False
    assert "not retransmitted" in (result.error or "")
    commander.write.assert_not_awaited()


async def test_completed_record_survives_service_reconstruction(tmp_path) -> None:
    """终态持久化后重建服务（模拟重启）：同 ID 同内容直接返回缓存结果，
    不再触达 Commander。"""
    commander = AsyncMock()
    commander.write.return_value = CommandResult(
        command_id="c12", outcome=WriteOutcome.CONFIRMED_SUCCESS
    )
    journal = DurableCommandRegistry(tmp_path / "commands.sqlite3")
    reads = AsyncMock()
    reads.read_point.return_value = _point_value(1.0)
    first_service = DeviceCommandService(commander, reads, InMemoryTrendStore(), journal)
    await first_service.send("d1", "setpoint", 1.0, command_id="c12")

    rebuilt = DeviceCommandService(AsyncMock(), AsyncMock(), InMemoryTrendStore(), journal)
    cached = await rebuilt.send("d1", "setpoint", 1.0, command_id="c12")

    assert cached.command_id == "c12"
    assert cached.outcome is WriteOutcome.CONFIRMED_SUCCESS
    commander.write.assert_awaited_once()
