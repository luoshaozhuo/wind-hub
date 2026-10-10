"""Protocol recovery contract: ensure-open before I/O, bounded read retries."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

import pytest

from core.application.errors import ProtocolConnectionError, ProtocolError
from core.application.protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
)
from core.application.recovery import RecoveringProtocol, RecoverySettings


class _Driver:
    def __init__(self, *, fail_connect: int = 0) -> None:
        self.connected = False
        self.fail_connect = fail_connect
        self.connect_count = 0
        self.read_count = 0
        self.write_count = 0
        self.drop_during_read = False
        self.drop_during_write = False
        self.fail_read: BaseException | None = None

    def is_open(self) -> bool:
        return self.connected

    def health(self) -> ConnectionHealth:
        return ConnectionHealth(healthy=self.connected)

    def capabilities(self) -> frozenset[ProtocolCapability]:
        return frozenset(
            {
                ProtocolCapability.READ,
                ProtocolCapability.WRITE,
                ProtocolCapability.WRITE_MANY,
            }
        )

    async def connect(self) -> None:
        self.connect_count += 1
        if self.connect_count <= self.fail_connect:
            raise ProtocolConnectionError("unreachable")
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def _read(
        self, point_ids: tuple[str, ...]
    ) -> tuple[ProtocolSample, ...]:
        self.read_count += 1
        if self.fail_read is not None and self.read_count == 1:
            raise self.fail_read
        if self.drop_during_read and self.read_count == 1:
            self.connected = False
            raise ProtocolConnectionError("connection lost")
        return tuple(
            ProtocolSample(point_id=p, value=42, quality=Quality.GOOD,
                           timestamp=datetime.now(UTC))
            for p in point_ids
        )

    async def read_one(self, point_id: str) -> ProtocolSample:
        return (await self._read((point_id,)))[0]

    async def read_many(self, point_ids: tuple[str, ...]) -> tuple[ProtocolSample, ...]:
        return await self._read(tuple(point_ids))

    async def _write(
        self, writes: tuple[ProtocolWrite, ...]
    ) -> tuple[ProtocolWriteResult, ...]:
        self.write_count += 1
        if self.drop_during_write:
            self.connected = False
            raise ProtocolConnectionError("lost after send")
        return tuple(ProtocolWriteResult(point_id=w.point_id, success=True)
                     for w in writes)

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        return (await self._write((write,)))[0]

    async def write_many(
        self, writes: tuple[ProtocolWrite, ...]
    ) -> tuple[ProtocolWriteResult, ...]:
        return await self._write(tuple(writes))


@pytest.mark.asyncio
async def test_open_connection_reads_without_connect() -> None:
    """连接已打开时首次读取直接执行，不调用 connect。"""
    driver = _Driver()
    driver.connected = True
    port = RecoveringProtocol(driver, RecoverySettings())
    assert (await port.read_one("a")).value == 42
    assert driver.connect_count == 0
    assert driver.read_count == 1


@pytest.mark.asyncio
async def test_closed_connection_connects_then_reads() -> None:
    """连接未打开：先 connect 后 read。"""
    driver = _Driver()
    port = RecoveringProtocol(driver, RecoverySettings())
    values = await port.read_many(("a",))
    assert values[0].value == 42
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_connect_failure_retries_up_to_read_retries() -> None:
    driver = _Driver(fail_connect=2)
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=3, retry_interval=0))
    assert (await port.read_one("a")).value == 42
    assert driver.connect_count == 3


@pytest.mark.asyncio
async def test_read_retries_zero_allows_first_attempt_only() -> None:
    """read_retries=0：首次必要的 connect 与首次 read 仍执行，失败不重试。"""
    driver = _Driver()
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=0))
    assert (await port.read_one("a")).value == 42
    assert driver.connect_count == 1

    driver = _Driver(fail_connect=1)
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=0))
    with pytest.raises(ProtocolError, match="unreachable"):
        await port.read_one("a")
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_read_failure_with_open_connection_retries_without_reconnect() -> None:
    """读失败但传输仍打开（可安全复用）：重试读取，不关闭/重建连接。"""
    driver = _Driver()
    driver.connected = True
    driver.fail_read = ProtocolConnectionError("no response")
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=1, retry_interval=0))
    assert (await port.read_one("a")).value == 42
    assert driver.read_count == 2
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_read_retries_after_detected_disconnect() -> None:
    """读中发现传输断开：按预算重连后重读。"""
    driver = _Driver()
    driver.connected = True
    driver.drop_during_read = True
    port = RecoveringProtocol(driver, RecoverySettings(retry_interval=0))
    assert (await port.read_one("a")).value == 42
    assert driver.read_count == 2
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_read_retries_exhausted_raises_last_error() -> None:
    """read_retries=1：首次 connect 后读失败，重试一次仍失败则抛出原始错误。"""
    driver = _Driver()
    driver.connected = True
    driver.fail_read = ProtocolConnectionError("no response")
    driver.drop_during_read = False

    async def always_fail(point_ids: tuple[str, ...]) -> tuple[ProtocolSample, ...]:
        driver.read_count += 1
        raise ProtocolConnectionError("no response")

    driver._read = always_fail  # type: ignore[method-assign]
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=1, retry_interval=0))
    with pytest.raises(ProtocolConnectionError, match="no response"):
        await port.read_one("a")
    assert driver.read_count == 2
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_unrecoverable_error_is_not_retried() -> None:
    """设备已应答的数据级 ProtocolError（如 Modbus 异常响应）：立即抛出。"""
    driver = _Driver()
    driver.connected = True
    driver.fail_read = ProtocolError("returned an exception response")
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=3, retry_interval=0))
    with pytest.raises(ProtocolError, match="exception response"):
        await port.read_one("a")
    assert driver.read_count == 1
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_retry_interval_is_observed_between_attempts() -> None:
    driver = _Driver()
    driver.connected = True
    driver.fail_read = ProtocolConnectionError("no response")
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=1, retry_interval=0.05))
    started = time.monotonic()
    assert (await port.read_one("a")).value == 42
    assert time.monotonic() - started >= 0.04


@pytest.mark.asyncio
async def test_infinite_retries_are_cancellable() -> None:
    """read_retries=-1：无限重试可被取消，CancelledError 原样传播。"""
    driver = _Driver(fail_connect=10**9)
    port = RecoveringProtocol(
        driver, RecoverySettings(read_retries=-1, retry_interval=0.01)
    )
    task = asyncio.create_task(port.read_one("a"))
    await asyncio.sleep(0.05)
    assert driver.connect_count > 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_close_terminates_infinite_retry_promptly() -> None:
    """无限重试期间 close：及时终止等待并抛出，不再重建连接。"""
    driver = _Driver(fail_connect=10**9)
    port = RecoveringProtocol(
        driver, RecoverySettings(read_retries=-1, retry_interval=30.0)
    )
    task = asyncio.create_task(port.read_one("a"))
    await asyncio.sleep(0.02)  # 进入重试等待
    await port.close()
    with pytest.raises(ProtocolError, match="closed"):
        await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_stale_retry_loop_does_not_cross_close_reconnect_boundary() -> None:
    """旧生命周期的无限重试在 close→显式 connect 后必须退出，不得借新连接执行 I/O。

    显式 connect 会复位 _closed/_close_event；仅靠这两个标志，关闭前启动的
    重试循环会在新连接上继续读取（跨越关闭—重连边界）。代际守卫使其退出。
    """
    driver = _Driver(fail_connect=10**9)
    port = RecoveringProtocol(
        driver, RecoverySettings(read_retries=-1, retry_interval=0.01)
    )
    task = asyncio.create_task(port.read_one("a"))
    await asyncio.sleep(0.05)  # 旧循环在 connect 失败的重试中
    assert driver.connect_count > 1

    await port.close()
    # 显式 connect 进入新生命周期：复位关闭标志并成功建连。
    driver.fail_connect = 0
    await port.connect()
    assert driver.is_open()

    with pytest.raises(ProtocolError, match="closed"):
        await asyncio.wait_for(task, timeout=1.0)
    # 旧循环从未在新连接上执行读取。
    assert driver.read_count == 0

    # 新生命周期的读取正常工作。
    sample = await port.read_one("a")
    assert sample.value == 42


@pytest.mark.asyncio
async def test_concurrent_close_and_connect_leave_consistent_state() -> None:
    """并发 close 与显式 connect 经连接锁串行化；无论谁先，结果状态一致可用。"""
    driver = _Driver()
    port = RecoveringProtocol(driver, RecoverySettings())
    await port.connect()
    await asyncio.gather(port.close(), port.connect())
    # 两种交错都合法（先 connect 后 close → 关闭；先 close 后 connect → 打开），
    # 但标志与 driver 状态必须一致，且不存在半初始化。
    assert port.is_open() == driver.is_open()
    # 后续显式 connect 后读取可用。
    await port.connect()
    assert (await port.read_one("a")).value == 42


@pytest.mark.asyncio
async def test_read_after_close_is_rejected() -> None:
    driver = _Driver()
    port = RecoveringProtocol(driver, RecoverySettings())
    await port.close()
    with pytest.raises(ProtocolError, match="closed"):
        await port.read_one("a")
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_read_many_retry_returns_whole_consistent_batch() -> None:
    """read_many 重试整体重读：结果只来自成功的尝试，顺序逐位对应。"""
    driver = _Driver()
    driver.connected = True
    driver.drop_during_read = True
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=1, retry_interval=0))
    samples = await port.read_many(("a", "b", "a"))
    assert [s.point_id for s in samples] == ["a", "b", "a"]
    assert all(s.value == 42 for s in samples)
    assert driver.read_count == 2


@pytest.mark.asyncio
async def test_write_is_not_resent_after_disconnect() -> None:
    driver = _Driver()
    driver.connected = True
    driver.drop_during_write = True
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=3, retry_interval=0))
    with pytest.raises(ProtocolError, match="lost after send"):
        await port.write_one(ProtocolWrite("control", 1))
    assert driver.write_count == 1
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_write_connects_before_first_send() -> None:
    driver = _Driver()
    port = RecoveringProtocol(driver, RecoverySettings())
    result = await port.write_one(ProtocolWrite("control", 1))
    assert result.success
    assert driver.connect_count == 1
    assert driver.write_count == 1


@pytest.mark.asyncio
async def test_write_connect_failure_is_not_retried() -> None:
    """写入的独立恢复策略：发送前连接失败只尝试一次，不套用读取重试。"""
    driver = _Driver(fail_connect=1)
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=-1, retry_interval=0))
    with pytest.raises(ProtocolError, match="unreachable"):
        await port.write_one(ProtocolWrite("control", 1))
    assert driver.connect_count == 1
    assert driver.write_count == 0


@pytest.mark.asyncio
async def test_concurrent_reads_share_single_reconnect() -> None:
    """并发请求同时发现断线：重连互斥，只建立一次连接。"""
    driver = _Driver()

    async def slow_connect() -> None:
        driver.connect_count += 1
        await asyncio.sleep(0.01)
        driver.connected = True

    driver.connect = slow_connect  # type: ignore[method-assign]
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=2, retry_interval=0))
    results = await asyncio.gather(*(port.read_one(f"p{i}") for i in range(8)))
    assert all(sample.value == 42 for sample in results)
    assert driver.connect_count == 1


def test_recovery_settings_validation() -> None:
    with pytest.raises(ValueError, match=">= -1"):
        RecoverySettings(read_retries=-2)
    with pytest.raises(ValueError, match=">= -1"):
        RecoverySettings(read_retries=True)
    with pytest.raises(ValueError, match="retry_interval"):
        RecoverySettings(retry_interval=-0.1)
    with pytest.raises(ValueError, match="retry_interval"):
        RecoverySettings(retry_interval=float("nan"))
    with pytest.raises(ValueError, match="retry_interval"):
        RecoverySettings(retry_interval=float("inf"))
    # 合法边界
    RecoverySettings(read_retries=-1)
    RecoverySettings(read_retries=0)
    RecoverySettings(retry_interval=0)


@pytest.mark.asyncio
async def test_read_timeout_is_enforced() -> None:
    driver = _Driver()
    driver.connected = True

    async def slow_read(point_ids: tuple[str, ...]) -> tuple[ProtocolSample, ...]:
        del point_ids
        await asyncio.sleep(0.05)
        return ()

    driver.read_many = slow_read  # type: ignore[method-assign]
    port = RecoveringProtocol(
        driver, RecoverySettings(read_timeout=0.001, read_retries=0)
    )
    with pytest.raises(ProtocolError, match="timed out"):
        await port.read_many(("a",))


@pytest.mark.asyncio
async def test_write_timeout_does_not_replay_command() -> None:
    driver = _Driver()
    driver.connected = True

    async def slow_write(write: ProtocolWrite) -> ProtocolWriteResult:
        del write
        driver.write_count += 1
        await asyncio.sleep(0.05)
        return ProtocolWriteResult(point_id="control", success=True)

    driver.write_one = slow_write  # type: ignore[method-assign]
    port = RecoveringProtocol(driver, RecoverySettings(write_timeout=0.001))
    with pytest.raises(ProtocolError, match="write timed out"):
        await port.write_one(ProtocolWrite("control", 5))
    assert driver.write_count == 1


@pytest.mark.asyncio
async def test_unsupported_write_many_propagates_without_fallback() -> None:
    """Driver 明确不支持 write_many 时：原样抛出，不降级为逐点写入。"""

    class _NoBatchDriver(_Driver):
        def capabilities(self) -> frozenset[ProtocolCapability]:
            return frozenset({ProtocolCapability.READ, ProtocolCapability.WRITE})

        async def write_many(
            self, writes: tuple[ProtocolWrite, ...]
        ) -> tuple[ProtocolWriteResult, ...]:
            raise NotImplementedError("write_many is not implemented")

    driver = _NoBatchDriver()
    driver.connected = True
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=3, retry_interval=0))
    with pytest.raises(NotImplementedError):
        await port.write_many((ProtocolWrite("control", 1),))
    with pytest.raises(NotImplementedError):
        await port.write_many(())
    assert driver.write_count == 0
    assert driver.connect_count == 0  # 已连接时不做任何额外动作

    # 断线状态下同样在重连之前拒绝，不做无意义的连接恢复。
    driver.connected = False
    with pytest.raises(NotImplementedError):
        await port.write_many((ProtocolWrite("control", 1),))
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_empty_read_many_returns_empty_without_connect() -> None:
    """空批量读取直接返回空 tuple，不触发连接恢复或 Driver 调用。"""
    driver = _Driver()
    port = RecoveringProtocol(driver, RecoverySettings())
    assert await port.read_many(()) == ()
    assert driver.connect_count == 0
    assert driver.read_count == 0


@pytest.mark.asyncio
async def test_empty_write_many_returns_empty_without_connect() -> None:
    """空批量写（已声明能力）直接返回空 tuple，不触发连接恢复。"""
    driver = _Driver()
    port = RecoveringProtocol(driver, RecoverySettings())
    assert await port.write_many(()) == ()
    assert driver.connect_count == 0
    assert driver.write_count == 0


@pytest.mark.asyncio
async def test_read_one_forwards_to_single_read_not_many() -> None:
    """单点读转发单点读：RecoveringProtocol 不得用 read_many 模拟 read_one。"""
    driver = _Driver()

    async def fail_read_many(point_ids: tuple[str, ...]) -> tuple[ProtocolSample, ...]:
        raise AssertionError(f"read_one must not be served by read_many: {point_ids}")

    driver.read_many = fail_read_many  # type: ignore[method-assign]
    port = RecoveringProtocol(driver, RecoverySettings())
    assert (await port.read_one("a")).value == 42


@pytest.mark.asyncio
async def test_write_many_forwards_to_batch_write_not_single() -> None:
    """批量写转发批量写：RecoveringProtocol 不得降级为逐点 write_one。"""
    driver = _Driver()
    driver.connected = True

    async def fail_write_one(write: ProtocolWrite) -> ProtocolWriteResult:
        raise AssertionError(f"write_many must not degrade to write_one: {write}")

    driver.write_one = fail_write_one  # type: ignore[method-assign]
    port = RecoveringProtocol(driver, RecoverySettings())
    results = await port.write_many((ProtocolWrite("a", 1), ProtocolWrite("b", 2)))
    assert [r.point_id for r in results] == ["a", "b"]
    assert driver.write_count == 1


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry() -> None:
    """读操作被取消：CancelledError 原样传播，不做恢复重试。"""
    driver = _Driver()
    driver.connected = True

    async def hanging_read(point_id: str) -> ProtocolSample:
        await asyncio.sleep(5)
        raise AssertionError("unreachable")

    driver.read_one = hanging_read  # type: ignore[method-assign]
    port = RecoveringProtocol(driver, RecoverySettings(read_timeout=None, read_retries=3))
    task = asyncio.create_task(port.read_one("a"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_reconnect_failure_leaves_no_half_initialized_state() -> None:
    """重连全部失败：抛出最后一次错误，且连接状态如实反映未连接。"""
    driver = _Driver(fail_connect=99)
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=2, retry_interval=0))
    with pytest.raises(ProtocolConnectionError, match="unreachable"):
        await port.read_one("a")
    assert driver.is_open() is False
    assert driver.connect_count == 3  # 首次尝试 + 2 次重试
    assert driver.read_count == 0


@pytest.mark.asyncio
async def test_connection_timeout_is_enforced() -> None:
    driver = _Driver()

    async def slow_connect() -> None:
        await asyncio.sleep(0.05)

    driver.connect = slow_connect  # type: ignore[method-assign]
    port = RecoveringProtocol(driver, RecoverySettings(connect_timeout=0.001))
    with pytest.raises(ProtocolError, match="connect timed out"):
        await port.connect()


@pytest.mark.asyncio
async def test_connect_is_idempotent_when_open() -> None:
    """已打开连接上的显式 connect 是 no-op，不重建底层连接。"""
    driver = _Driver()
    driver.connected = True
    port = RecoveringProtocol(driver, RecoverySettings())
    await port.connect()
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_external_connect_serialized_with_transparent_restore() -> None:
    """读路径透明恢复在途时，外部 connect 被串行化——同一断线只建一次连接。"""
    driver = _Driver()
    connect_entered = asyncio.Event()

    async def slow_connect() -> None:
        driver.connect_count += 1
        connect_entered.set()
        await asyncio.sleep(0.02)
        driver.connected = True

    driver.connect = slow_connect  # type: ignore[method-assign]
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=2, retry_interval=0))

    read_task = asyncio.create_task(port.read_one("a"))
    await connect_entered.wait()
    await port.connect()  # 等待恢复完成后发现已打开，直接返回
    assert (await read_task).value == 42
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_close_waits_for_inflight_restore_then_closes() -> None:
    """恢复在途时调用 close：先等恢复完成，再关闭——恢复不会晚于 close 重建连接。"""
    driver = _Driver()
    connect_entered = asyncio.Event()

    async def slow_connect() -> None:
        driver.connect_count += 1
        connect_entered.set()
        await asyncio.sleep(0.02)
        driver.connected = True

    driver.connect = slow_connect  # type: ignore[method-assign]
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=2, retry_interval=0))

    read_task = asyncio.create_task(port.read_one("a"))
    await connect_entered.wait()
    await port.close()
    assert (await read_task).value == 42  # 在途读已完成，不受 close 影响
    assert driver.is_open() is False
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_reconnect_hook_fires_once_per_transparent_reconnect() -> None:
    driver = _Driver()
    driver.connected = True
    driver.drop_during_read = True
    port = RecoveringProtocol(driver, RecoverySettings(retry_interval=0))
    events: list[str] = []
    port.set_reconnect_hook(lambda: events.append("reconnected"))

    assert (await port.read_one("a")).value == 42
    assert events == ["reconnected"]

    # 已打开的连接不触发 hook。
    assert (await port.read_one("a")).value == 42
    assert events == ["reconnected"]


@pytest.mark.asyncio
async def test_reconnect_hook_not_fired_on_failed_reconnect() -> None:
    driver = _Driver(fail_connect=5)
    port = RecoveringProtocol(driver, RecoverySettings(read_retries=2, retry_interval=0))
    events: list[str] = []
    port.set_reconnect_hook(lambda: events.append("reconnected"))

    with pytest.raises(ProtocolError, match="unreachable"):
        await port.read_one("a")
    assert events == []


@pytest.mark.asyncio
async def test_reconnect_hook_exception_does_not_break_read() -> None:
    driver = _Driver()
    port = RecoveringProtocol(driver, RecoverySettings())

    def bad_hook() -> None:
        raise RuntimeError("hook boom")

    port.set_reconnect_hook(bad_hook)
    assert (await port.read_one("a")).value == 42
