"""新 Commander CommandDispatcher 幂等/超时/错误收敛单元测试。"""

from __future__ import annotations

import asyncio
import contextlib

from commander.application.command import Command
from commander.application.dispatcher import CommandDispatcher
from commander.application.runtime import CommanderRuntime
from core.application.protocol_contract import ProtocolWriteResult, WriteOutcome
from tests.support.new_commander import FakeRegistry, make_commander_config


def _runtime(registry: FakeRegistry | None = None) -> CommanderRuntime:
    return CommanderRuntime(
        make_commander_config(),
        config_hash="hash",
        protocol_registry=registry or FakeRegistry(),
    )


async def test_send_success_and_idempotent_replay():
    registry = FakeRegistry()
    dispatcher = CommandDispatcher(_runtime(registry))
    command = Command(command_id="c1", device_id="dev1", point_id="p1", value=1.0)

    first = await dispatcher.send(command)
    assert first.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert len(registry.instances[0].writes) == 1

    replay = await dispatcher.send(command)
    assert replay.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert len(registry.instances[0].writes) == 1  # 幂等命中，未重复写


async def test_idempotency_conflict_on_different_payload():
    dispatcher = CommandDispatcher(_runtime())
    command = Command(command_id="c1", device_id="dev1", point_id="p1", value=1.0)
    await dispatcher.send(command)

    conflict = await dispatcher.send(
        Command(command_id="c1", device_id="dev1", point_id="p1", value=2.0)
    )
    assert conflict.outcome is WriteOutcome.CONFIRMED_FAILURE
    assert "idempotency conflict" in (conflict.error or "")


async def test_unknown_device_returns_failure_result():
    dispatcher = CommandDispatcher(_runtime())
    result = await dispatcher.send(
        Command(command_id="c2", device_id="ghost", point_id="p1", value=1.0)
    )
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    assert "unknown device" in (result.error or "")


async def test_write_timeout_returns_unknown_result():
    registry = FakeRegistry()

    async def slow_write(write):
        await asyncio.sleep(5)
        return ProtocolWriteResult(point_id=write.point_id, outcome=WriteOutcome.CONFIRMED_SUCCESS)

    runtime = _runtime(registry)
    protocol = runtime.device("dev1").protocol
    protocol.write_one = slow_write  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    dispatcher = CommandDispatcher(runtime)
    result = await dispatcher.send(
        Command(
            command_id="c3",
            device_id="dev1",
            point_id="p1",
            value=1.0,
            timeout=0.05,
        )
    )
    # 写超时：命令可能已送达设备，结果必须表达为 UNKNOWN 而非确认失败。
    assert result.outcome is WriteOutcome.UNKNOWN
    assert "timeout" in (result.error or "")


async def test_concurrent_same_command_shares_inflight():
    registry = FakeRegistry()
    dispatcher = CommandDispatcher(_runtime(registry))
    command = Command(command_id="c4", device_id="dev1", point_id="p1", value=1.0)
    first, second = await asyncio.gather(dispatcher.send(command), dispatcher.send(command))
    assert first.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert second.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert len(registry.instances[0].writes) == 1


async def test_send_batch_preserves_order_and_isolates_failures():
    """批量写并发执行：单条失败只收敛在自身结果，按输入顺序返回。"""
    registry = FakeRegistry()
    dispatcher = CommandDispatcher(_runtime(registry))
    results = await dispatcher.send_batch(
        [
            Command(command_id="b1", device_id="ghost", point_id="p1", value=1.0),
            Command(command_id="b2", device_id="dev1", point_id="p1", value=1.0),
        ]
    )
    assert [result.command_id for result in results] == ["b1", "b2"]
    assert results[0].outcome is WriteOutcome.CONFIRMED_FAILURE
    assert "unknown device" in (results[0].error or "")
    assert results[1].outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert len(registry.instances[0].writes) == 1


async def test_protocol_rejection_maps_to_confirmed_failure():
    registry = FakeRegistry()
    runtime = _runtime(registry)
    runtime.device("dev1").protocol.write_success = False  # type: ignore[attr-defined]
    dispatcher = CommandDispatcher(runtime)
    result = await dispatcher.send(
        Command(command_id="c5", device_id="dev1", point_id="p1", value=1.0)
    )
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    assert "rejected" in (result.error or "")


async def test_client_cancellation_does_not_cancel_inflight_write():
    """调用方被取消：底层写入经 shield 保护仍完成，幂等缓存可回放结果。"""
    registry = FakeRegistry()
    dispatcher = CommandDispatcher(_runtime(registry))

    write_release = asyncio.Event()
    original_write_one = registry.instances[0].write_one

    async def slow_write(write):
        await write_release.wait()
        return await original_write_one(write)

    registry.instances[0].write_one = slow_write  # type: ignore[method-assign]
    command = Command(command_id="c-cancel", device_id="dev1", point_id="p1", value=1.0)

    caller = asyncio.create_task(dispatcher.send(command))
    await asyncio.sleep(0.05)
    caller.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await caller

    write_release.set()
    await asyncio.sleep(0.05)
    assert len(registry.instances[0].writes) == 1  # 写入未被取消

    replay = await dispatcher.send(command)
    assert replay.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert len(registry.instances[0].writes) == 1  # 缓存回放，未重复写


async def test_idempotency_cache_spans_generation_switch():
    """跨 generation 语义（现状固定）：activate 后同 command_id 仍回放旧结果。

    与旧 Commander 一致——幂等缓存是进程级 Dispatcher 持有，不随
    generation 切换失效；是否应跨配置版本有效属产品策略，此处仅锁定
    现有行为。
    """
    registry = FakeRegistry()
    runtime = _runtime(registry)
    dispatcher = CommandDispatcher(runtime)
    command = Command(command_id="c-gen", device_id="dev1", point_id="p1", value=1.0)

    first = await dispatcher.send(command)
    assert first.outcome is WriteOutcome.CONFIRMED_SUCCESS

    await runtime.prepare_config("r2", make_commander_config(), "hash-b")
    await runtime.activate_config("r2")

    replay = await dispatcher.send(command)
    assert replay.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert len(registry.instances) == 2  # 新 generation 新会话
    assert len(registry.instances[1].writes) == 0  # 未写入新会话


async def test_inflight_write_uses_pinned_generation_during_activate():
    """activate 期间的在途写固定在旧 generation 完成，不被切走。"""
    registry = FakeRegistry()
    runtime = _runtime(registry)
    dispatcher = CommandDispatcher(runtime)

    write_release = asyncio.Event()
    original_write_one = registry.instances[0].write_one

    async def slow_write(write):
        await write_release.wait()
        return await original_write_one(write)

    registry.instances[0].write_one = slow_write  # type: ignore[method-assign]
    command = Command(command_id="c-pin", device_id="dev1", point_id="p1", value=1.0)

    in_flight = asyncio.create_task(dispatcher.send(command))
    await asyncio.sleep(0.05)
    await runtime.prepare_config("r2", make_commander_config(), "hash-b")
    await runtime.activate_config("r2")
    write_release.set()

    result = await in_flight
    assert result.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert len(registry.instances[0].writes) == 1  # 写入落在旧 generation 会话
    assert len(registry.instances[1].writes) == 0


async def test_protocol_error_during_write_returns_unknown_result():
    """写入期间连接/协议异常：设备侧状态不可知，结果必须是 UNKNOWN。"""
    from core.application.errors import ProtocolConnectionError

    registry = FakeRegistry()

    async def failing_write(write):
        raise ProtocolConnectionError("connection reset during write")

    runtime = _runtime(registry)
    protocol = runtime.device("dev1").protocol
    protocol.write_one = failing_write  # type: ignore[method-assign]
    dispatcher = CommandDispatcher(runtime)
    result = await dispatcher.send(
        Command(command_id="c-io", device_id="dev1", point_id="p1", value=1.0)
    )
    assert result.outcome is WriteOutcome.UNKNOWN
    assert "connection reset" in (result.error or "")


async def test_uncertain_driver_result_maps_to_unknown():
    """驱动返回 UNKNOWN（如 IEC104 控制确认超时）透传为 UNKNOWN。"""
    registry = FakeRegistry()

    async def uncertain_write(write):
        return ProtocolWriteResult(
            point_id=write.point_id,
            outcome=WriteOutcome.UNKNOWN,
            message="activation confirm timeout",
        )

    runtime = _runtime(registry)
    protocol = runtime.device("dev1").protocol
    protocol.write_one = uncertain_write  # type: ignore[method-assign]
    dispatcher = CommandDispatcher(runtime)
    result = await dispatcher.send(
        Command(command_id="c-unc", device_id="dev1", point_id="p1", value=1.0)
    )
    assert result.outcome is WriteOutcome.UNKNOWN
    assert "activation confirm timeout" in (result.error or "")


async def test_not_connected_returns_confirmed_failure():
    """写入前建连失败：命令未发送，确证未执行（CONFIRMED_FAILURE）。"""
    registry = FakeRegistry()
    runtime = _runtime(registry)
    registry.instances[0].fail_connect = True
    dispatcher = CommandDispatcher(runtime)
    result = await dispatcher.send(
        Command(command_id="c-nc", device_id="dev1", point_id="p1", value=1.0)
    )
    assert result.outcome is WriteOutcome.CONFIRMED_FAILURE
    assert "not connected" in (result.error or "")
    assert len(registry.instances[0].writes) == 0


async def test_unknown_timeout_is_cached_without_second_physical_write() -> None:
    """超时后的相同 command_id 不能重新调用设备写入。"""
    registry = FakeRegistry()
    runtime = _runtime(registry)
    entered = 0
    never_complete = asyncio.Event()

    async def suspended_write(write):
        nonlocal entered
        entered += 1
        await never_complete.wait()
        return ProtocolWriteResult(point_id=write.point_id, outcome=WriteOutcome.CONFIRMED_SUCCESS)

    runtime.device("dev1").protocol.write_one = suspended_write  # type: ignore[method-assign]
    dispatcher = CommandDispatcher(runtime)
    command = Command(command_id="no-retry", device_id="dev1", point_id="p1", value=1.0, timeout=0.02)
    first = await dispatcher.send(command)
    replay = await dispatcher.send(command)
    assert first.outcome is WriteOutcome.UNKNOWN
    assert replay.outcome is WriteOutcome.UNKNOWN
    assert entered == 1
    await runtime.stop()


async def test_idempotency_cache_is_not_shared_by_new_dispatcher() -> None:
    """重启语义：不同 Dispatcher 实例不共享进程内去重缓存。"""
    registry = FakeRegistry()
    runtime = _runtime(registry)
    command = Command(command_id="restart", device_id="dev1", point_id="p1", value=1.0)
    first = await CommandDispatcher(runtime).send(command)
    second = await CommandDispatcher(runtime).send(command)
    assert first.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert second.outcome is WriteOutcome.CONFIRMED_SUCCESS
    assert len(registry.instances[0].writes) == 2
    await runtime.stop()
