"""新 Commander CommandDispatcher 幂等/超时/错误收敛单元测试。"""

from __future__ import annotations

import asyncio

from commander.application.command import Command
from commander.application.dispatcher import CommandDispatcher
from commander.application.runtime import CommanderRuntime
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

    async def slow_write(writes):
        await asyncio.sleep(5)
        return ()

    runtime = _runtime(registry)
    runtime.device("dev1").protocol.write = slow_write  # type: ignore[attr-defined]
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
