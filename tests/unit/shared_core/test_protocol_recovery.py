"""Protocol recovery contract: reconnect before I/O, never resend controls."""

from __future__ import annotations

import asyncio

import pytest

from core.application.errors import ProtocolError
from core.application.protocol_contract import (
    ConnectionHealth,
    ProtocolCapability,
    ProtocolSample,
    ProtocolWrite,
    ProtocolWriteResult,
    Quality,
)
from core.application.recovery import RecoveryPort, RecoverySettings


class _Driver:
    def __init__(self, *, fail_connect: int = 0) -> None:
        self.connected = False
        self.fail_connect = fail_connect
        self.connect_count = 0
        self.read_count = 0
        self.write_count = 0
        self.drop_during_read = False
        self.drop_during_write = False

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
            raise ProtocolError("unreachable")
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def _read(
        self, point_ids: tuple[str, ...]
    ) -> tuple[ProtocolSample, ...]:
        self.read_count += 1
        if self.drop_during_read and self.read_count == 1:
            self.connected = False
            raise ProtocolError("connection lost")
        return tuple(ProtocolSample(point_id=p, value=42, quality=Quality.GOOD)
                     for p in point_ids)

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
            raise ProtocolError("lost after send")
        return tuple(ProtocolWriteResult(point_id=w.point_id, success=True)
                     for w in writes)

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult:
        return (await self._write((write,)))[0]

    async def write_many(
        self, writes: tuple[ProtocolWrite, ...]
    ) -> tuple[ProtocolWriteResult, ...]:
        return await self._write(tuple(writes))


@pytest.mark.asyncio
async def test_read_recovers_with_one_default_attempt() -> None:
    driver = _Driver()
    port = RecoveryPort(driver, RecoverySettings())
    values = await port.read_many(("a",))
    assert values[0].value == 42
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_retries_connection_up_to_configured_count() -> None:
    driver = _Driver(fail_connect=2)
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=3))
    assert (await port.read_one("a")).value == 42
    assert driver.connect_count == 3


@pytest.mark.asyncio
async def test_disabled_recovery_does_not_try_connect() -> None:
    driver = _Driver()
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=0))
    with pytest.raises(ProtocolError, match="0 reconnect"):
        await port.read_one("a")
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_read_retries_after_detected_disconnect() -> None:
    driver = _Driver()
    driver.connected = True
    driver.drop_during_read = True
    port = RecoveryPort(driver, RecoverySettings())
    assert (await port.read_one("a")).value == 42
    assert driver.read_count == 2
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_write_is_not_resent_after_disconnect() -> None:
    driver = _Driver()
    driver.connected = True
    driver.drop_during_write = True
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=3))
    with pytest.raises(ProtocolError, match="lost after send"):
        await port.write_one(ProtocolWrite("control", 1))
    assert driver.write_count == 1
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_write_connects_before_first_send() -> None:
    driver = _Driver()
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=1))
    result = await port.write_one(ProtocolWrite("control", 1))
    assert result.success
    assert driver.connect_count == 1
    assert driver.write_count == 1


@pytest.mark.asyncio
async def test_concurrent_reads_share_single_reconnect() -> None:
    """并发请求同时发现断线：重连互斥，只建立一次连接。"""
    driver = _Driver()

    async def slow_connect() -> None:
        driver.connect_count += 1
        await asyncio.sleep(0.01)
        driver.connected = True

    driver.connect = slow_connect  # type: ignore[method-assign]
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=2))
    results = await asyncio.gather(*(port.read_one(f"p{i}") for i in range(8)))
    assert all(sample.value == 42 for sample in results)
    assert driver.connect_count == 1


def test_recovery_configuration_rejects_invalid_attempts() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        RecoverySettings(reconnect_attempts=-1)
    with pytest.raises(ValueError, match="nonnegative"):
        RecoverySettings(reconnect_attempts=True)


@pytest.mark.asyncio
async def test_read_timeout_is_enforced() -> None:
    driver = _Driver()
    driver.connected = True

    async def slow_read(point_ids: tuple[str, ...]) -> tuple[ProtocolSample, ...]:
        del point_ids
        await asyncio.sleep(0.05)
        return ()

    driver.read_many = slow_read  # type: ignore[method-assign]
    port = RecoveryPort(driver, RecoverySettings(read_timeout=0.001))
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
    port = RecoveryPort(driver, RecoverySettings(write_timeout=0.001))
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
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=3))
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
    port = RecoveryPort(driver, RecoverySettings())
    assert await port.read_many(()) == ()
    assert driver.connect_count == 0
    assert driver.read_count == 0


@pytest.mark.asyncio
async def test_empty_write_many_returns_empty_without_connect() -> None:
    """空批量写（已声明能力）直接返回空 tuple，不触发连接恢复。"""
    driver = _Driver()
    port = RecoveryPort(driver, RecoverySettings())
    assert await port.write_many(()) == ()
    assert driver.connect_count == 0
    assert driver.write_count == 0


@pytest.mark.asyncio
async def test_read_one_forwards_to_single_read_not_many() -> None:
    """单点读转发单点读：RecoveryPort 不得用 read_many 模拟 read_one。"""
    driver = _Driver()

    async def fail_read_many(point_ids: tuple[str, ...]) -> tuple[ProtocolSample, ...]:
        raise AssertionError(f"read_one must not be served by read_many: {point_ids}")

    driver.read_many = fail_read_many  # type: ignore[method-assign]
    port = RecoveryPort(driver, RecoverySettings())
    assert (await port.read_one("a")).value == 42


@pytest.mark.asyncio
async def test_write_many_forwards_to_batch_write_not_single() -> None:
    """批量写转发批量写：RecoveryPort 不得降级为逐点 write_one。"""
    driver = _Driver()
    driver.connected = True

    async def fail_write_one(write: ProtocolWrite) -> ProtocolWriteResult:
        raise AssertionError(f"write_many must not degrade to write_one: {write}")

    driver.write_one = fail_write_one  # type: ignore[method-assign]
    port = RecoveryPort(driver, RecoverySettings())
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
    port = RecoveryPort(driver, RecoverySettings(read_timeout=None, reconnect_attempts=3))
    task = asyncio.create_task(port.read_one("a"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert driver.connect_count == 0


@pytest.mark.asyncio
async def test_reconnect_failure_leaves_no_half_initialized_state() -> None:
    """重连全部失败：抛 ProtocolError，且健康状态如实反映未连接。"""
    driver = _Driver(fail_connect=99)
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=2))
    with pytest.raises(ProtocolError, match="2 reconnect"):
        await port.read_one("a")
    assert driver.health().healthy is False
    assert driver.connect_count == 2
    assert driver.read_count == 0


@pytest.mark.asyncio
async def test_close_failure_during_restore_still_attempts_connect() -> None:
    """恢复路径中 close 失败计入一次尝试，剩余尝试继续，不留失效连接。"""
    driver = _Driver()
    close_calls = 0

    async def flaky_close() -> None:
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise ProtocolError("close blew up")
        driver.connected = False

    driver.close = flaky_close  # type: ignore[method-assign]
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=2))
    assert (await port.read_one("a")).value == 42
    assert close_calls == 2
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_connection_timeout_is_enforced() -> None:
    driver = _Driver()

    async def slow_connect() -> None:
        await asyncio.sleep(0.05)

    driver.connect = slow_connect  # type: ignore[method-assign]
    port = RecoveryPort(driver, RecoverySettings(connect_timeout=0.001))
    with pytest.raises(ProtocolError, match="connect timed out"):
        await port.connect()


@pytest.mark.asyncio
async def test_logical_read_shares_reconnect_budget_across_phases() -> None:
    """一次逻辑读取的恢复预算为 reconnect_attempts 总数：

    读前恢复已消耗唯一一次预算后，读中再断线不得触发第二次重连——
    原始读取错误原样传播，不允许预算翻倍。
    """
    driver = _Driver()
    driver.drop_during_read = True  # 第一次 read 掉线并抛错
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=1))
    with pytest.raises(ProtocolError, match="connection lost"):
        await port.read_one("a")
    assert driver.connect_count == 1  # 预算已在读前恢复中耗尽
    assert driver.read_count == 1


@pytest.mark.asyncio
async def test_post_failure_restore_uses_remaining_budget() -> None:
    """reconnect_attempts=2：读前恢复用 1 次，读失败后剩余 1 次可再恢复。"""
    driver = _Driver()
    driver.drop_during_read = True
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=2))
    assert (await port.read_one("a")).value == 42
    assert driver.connect_count == 2
    assert driver.read_count == 2


@pytest.mark.asyncio
async def test_connect_is_idempotent_when_healthy() -> None:
    """健康连接上的显式 connect 是 no-op，不重建底层连接。"""
    driver = _Driver()
    driver.connected = True
    port = RecoveryPort(driver, RecoverySettings())
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
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=2))

    read_task = asyncio.create_task(port.read_one("a"))
    await connect_entered.wait()
    await port.connect()  # 等待恢复完成后发现已健康，直接返回
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
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=2))

    read_task = asyncio.create_task(port.read_one("a"))
    await connect_entered.wait()
    await port.close()
    assert (await read_task).value == 42  # 在途读已完成，不受 close 影响
    assert driver.health().healthy is False
    assert driver.connect_count == 1


@pytest.mark.asyncio
async def test_reconnect_hook_fires_once_per_transparent_reconnect() -> None:
    driver = _Driver()
    driver.connected = True
    driver.drop_during_read = True
    port = RecoveryPort(driver, RecoverySettings())
    events: list[str] = []
    port.set_reconnect_hook(lambda: events.append("reconnected"))

    assert (await port.read_one("a")).value == 42
    assert events == ["reconnected"]

    # 已健康的连接不触发 hook。
    assert (await port.read_one("a")).value == 42
    assert events == ["reconnected"]


@pytest.mark.asyncio
async def test_reconnect_hook_not_fired_on_failed_reconnect() -> None:
    driver = _Driver(fail_connect=5)
    port = RecoveryPort(driver, RecoverySettings(reconnect_attempts=2))
    events: list[str] = []
    port.set_reconnect_hook(lambda: events.append("reconnected"))

    with pytest.raises(ProtocolError, match="reconnect"):
        await port.read_one("a")
    assert events == []


@pytest.mark.asyncio
async def test_reconnect_hook_exception_does_not_break_read() -> None:
    driver = _Driver()
    port = RecoveryPort(driver, RecoverySettings())

    def bad_hook() -> None:
        raise RuntimeError("hook boom")

    port.set_reconnect_hook(bad_hook)
    assert (await port.read_one("a")).value == 42
