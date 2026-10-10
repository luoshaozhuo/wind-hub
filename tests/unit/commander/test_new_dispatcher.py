"""新 Commander CommandDispatcher 幂等/超时/错误收敛单元测试。"""

from __future__ import annotations

import asyncio
import contextlib

from commander.application.command import Command
from commander.application.dispatcher import CommandDispatcher
from commander.application.runtime import CommanderRuntime
from core.application.protocol_contract import ProtocolWriteResult
from tests.support.new_commander import FakeRegistry, make_commander_config


def _runtime(registry: FakeRegistry | None = None) -> CommanderRuntime:
    return CommanderRuntime(
        make_commander_config(),
        config_hash="hash",
        protocol_registry=registry or FakeRegistry(),  # type: ignore[arg-type]
    )


async def test_send_success_and_idempotent_replay():
    registry = FakeRegistry()
    dispatcher = CommandDispatcher(_runtime(registry))
    command = Command(command_id="c1", device_id="dev1", point_id="p1", value=1.0)

    first = await dispatcher.send(command)
    assert first.success
    assert len(registry.instances[0].writes) == 1

    replay = await dispatcher.send(command)
    assert replay.success
    assert len(registry.instances[0].writes) == 1  # 幂等命中，未重复写


async def test_idempotency_conflict_on_different_payload():
    dispatcher = CommandDispatcher(_runtime())
    command = Command(command_id="c1", device_id="dev1", point_id="p1", value=1.0)
    await dispatcher.send(command)

    conflict = await dispatcher.send(
        Command(command_id="c1", device_id="dev1", point_id="p1", value=2.0)
    )
    assert not conflict.success
    assert "idempotency conflict" in (conflict.error or "")


async def test_unknown_device_returns_failure_result():
    dispatcher = CommandDispatcher(_runtime())
    result = await dispatcher.send(
        Command(command_id="c2", device_id="ghost", point_id="p1", value=1.0)
    )
    assert not result.success
    assert "unknown device" in (result.error or "")


async def test_write_timeout_returns_failure_result():
    registry = FakeRegistry()

    async def slow_write(write):
        await asyncio.sleep(5)
        return ProtocolWriteResult(point_id=write.point_id, success=True)

    runtime = _runtime(registry)
    runtime.device("dev1").protocol.write_one = slow_write  # type: ignore[attr-defined]
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
    assert not result.success
    assert "timeout" in (result.error or "")


async def test_concurrent_same_command_shares_inflight():
    registry = FakeRegistry()
    dispatcher = CommandDispatcher(_runtime(registry))
    command = Command(command_id="c4", device_id="dev1", point_id="p1", value=1.0)
    first, second = await asyncio.gather(dispatcher.send(command), dispatcher.send(command))
    assert first.success and second.success
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
    assert not results[0].success
    assert "unknown device" in (results[0].error or "")
    assert results[1].success
    assert len(registry.instances[0].writes) == 1


async def test_protocol_rejection_maps_to_failure_result():
    registry = FakeRegistry()
    runtime = _runtime(registry)
    runtime.device("dev1").protocol.write_success = False  # type: ignore[attr-defined]
    dispatcher = CommandDispatcher(runtime)
    result = await dispatcher.send(
        Command(command_id="c5", device_id="dev1", point_id="p1", value=1.0)
    )
    assert not result.success
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
    assert replay.success
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
    assert first.success

    await runtime.prepare_config("r2", make_commander_config(), "hash-b")
    await runtime.activate_config("r2")

    replay = await dispatcher.send(command)
    assert replay.success
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
    assert result.success
    assert len(registry.instances[0].writes) == 1  # 写入落在旧 generation 会话
    assert len(registry.instances[1].writes) == 0
