"""新 Commander Runtime generation 生命周期组件测试。"""

from __future__ import annotations

import asyncio

import pytest

from commander.application.runtime import CommanderRuntime
from commander.application.services import (
    CommanderConfigService,
    CommanderReadService,
)
from tests.support.new_commander import FakeRegistry, make_commander_config


def _runtime(registry: FakeRegistry | None = None) -> CommanderRuntime:
    return CommanderRuntime(
        make_commander_config(),
        config_hash="hash-a",
        protocol_registry=registry or FakeRegistry(),  # type: ignore[arg-type]
    )


async def test_lazy_connect_on_first_operation():
    registry = FakeRegistry()
    runtime = _runtime(registry)
    assert registry.instances[0].connect_calls == 0

    assert await runtime.ensure_connected("dev1")
    assert registry.instances[0].connect_calls == 1

    # 健康快路径：已连接不重复 connect
    assert await runtime.ensure_connected("dev1")
    assert registry.instances[0].connect_calls == 1


async def test_ensure_connected_failure_returns_false():
    registry = FakeRegistry()
    runtime = _runtime(registry)
    protocol = registry.instances[0]
    protocol.fail_connect = True
    assert not await runtime.ensure_connected("dev1")
    assert protocol.close_calls == 1  # 重连前先关闭旧会话


async def test_concurrent_ensure_connected_serialized():
    registry = FakeRegistry()
    runtime = _runtime(registry)
    results = await asyncio.gather(*(runtime.ensure_connected("dev1") for _ in range(8)))
    assert all(results)
    assert registry.instances[0].connect_calls == 1


async def test_read_service_end_to_end():
    registry = FakeRegistry()
    runtime = _runtime(registry)
    registry.instances[0].read_values["p1"] = 42.0
    service = CommanderReadService(runtime)

    reading = await service.read_point("dev1", "p1")
    assert reading.value == 42.0
    assert reading.device_id == "dev1"


async def test_read_service_unknown_device_and_point():
    runtime = _runtime()
    service = CommanderReadService(runtime)
    with pytest.raises(Exception, match="unknown device"):
        await service.read_point("ghost", "p1")
    with pytest.raises(Exception, match="unknown points"):
        await service.read_points("dev1", ["p1", "ghost"])
    with pytest.raises(Exception, match="non-empty"):
        await service.read_points("dev1", [])


async def test_prepare_activate_switches_generation_and_retires_old():
    registry = FakeRegistry()
    runtime = _runtime(registry)
    old_protocol = registry.instances[0]

    candidate = make_commander_config()
    await runtime.prepare_config("rev-2", candidate, "hash-b")
    assert runtime.prepared_revision == "rev-2"
    assert len(registry.instances) == 2

    await runtime.activate_config("rev-2")
    assert runtime.active_revision == "rev-2"
    assert runtime.active_config_hash == "hash-b"
    assert runtime.prepared_revision is None

    # 旧 generation 无在途操作 → 回收任务异步完成关闭
    await asyncio.gather(*list(runtime._retirement_tasks), return_exceptions=True)
    assert old_protocol.close_calls == 1


async def test_activate_retirement_waits_for_inflight_operation():
    registry = FakeRegistry()
    runtime = _runtime(registry)
    old_protocol = registry.instances[0]

    release = asyncio.Event()

    async def pinned_operation():
        async with runtime.operation():
            await release.wait()

    task = asyncio.create_task(pinned_operation())
    await asyncio.sleep(0)  # 让操作进入 pinned 状态

    await runtime.reload(
        make_commander_config(),
        config_hash="hash-b",
        revision_id="rev-2",
    )
    await asyncio.sleep(0.05)
    # 旧 generation 仍有在途操作 → 尚未关闭
    assert old_protocol.close_calls == 0

    release.set()
    await asyncio.gather(task, *list(runtime._retirement_tasks))
    assert old_protocol.close_calls == 1


async def test_operation_pins_generation_during_activate():
    registry = FakeRegistry()
    runtime = _runtime(registry)
    old_protocol = registry.instances[0]
    old_protocol.connected = True

    async with runtime.operation():
        await runtime.reload(
            make_commander_config(),
            config_hash="hash-b",
            revision_id="rev-2",
        )
        # operation 固定旧 generation：读到的仍是旧会话
        assert runtime.device("dev1").protocol is old_protocol

    assert runtime.device("dev1").protocol is registry.instances[1]


async def test_abort_config_idempotent():
    runtime = _runtime()
    await runtime.prepare_config("rev-2", make_commander_config(), "hash-b")
    assert await runtime.abort_config("rev-2")
    assert runtime.prepared_revision is None
    assert not await runtime.abort_config("rev-2")
    assert not await runtime.abort_config("other")


async def test_activate_without_prepare_rejected():
    runtime = _runtime()
    with pytest.raises(ValueError, match="prepared revision mismatch"):
        await runtime.activate_config("rev-x")


async def test_stop_closes_all_generations():
    registry = FakeRegistry()
    runtime = _runtime(registry)
    await runtime.prepare_config("rev-2", make_commander_config(), "hash-b")
    await runtime.stop()
    for protocol in registry.instances:
        assert protocol.close_calls == 1


async def test_config_service_toctou_and_hash_check(tmp_path):
    runtime = _runtime()
    load_calls = 0

    def fake_load(path):
        nonlocal load_calls
        load_calls += 1
        return make_commander_config()

    hashes = iter(["h1", "h1"])

    service = CommanderConfigService(
        tmp_path,
        runtime,
        load_config=fake_load,  # type: ignore[arg-type]
        fingerprint=lambda path: next(hashes),  # type: ignore[arg-type]
    )
    actual = await service.prepare_config("rev-3", "h1")
    assert actual == "h1"
    assert runtime.prepared_revision == "rev-3"

    # 期望 hash 不匹配
    service2 = CommanderConfigService(
        tmp_path,
        runtime,
        load_config=fake_load,  # type: ignore[arg-type]
        fingerprint=lambda path: "hX",  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="hash mismatch"):
        await service2.prepare_config("rev-4", "hY")
    await runtime.stop()


async def test_config_service_detects_change_during_load(tmp_path):
    runtime = _runtime()
    hashes = iter(["before", "after"])
    service = CommanderConfigService(
        tmp_path,
        runtime,
        load_config=lambda path: make_commander_config(),  # type: ignore[arg-type]
        fingerprint=lambda path: next(hashes),  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="config changed while preparing"):
        await service.prepare_config("rev-5", "before")


async def test_prepare_hook_failure_degrades_not_raises():
    async def bad_hook(config):
        raise RuntimeError("ads init failed")

    runtime = CommanderRuntime(
        make_commander_config(),
        config_hash="hash-a",
        protocol_registry=FakeRegistry(),  # type: ignore[arg-type]
        prepare_protocols=bad_hook,
    )
    await runtime.start()  # 不抛出，降级继续


async def test_prepare_hook_called_on_activate():
    calls: list[str] = []

    async def hook(config):
        calls.append("called")

    registry = FakeRegistry()
    runtime = CommanderRuntime(
        make_commander_config(),
        config_hash="hash-a",
        protocol_registry=registry,  # type: ignore[arg-type]
        prepare_protocols=hook,
    )
    await runtime.reload(make_commander_config(), config_hash="hash-b", revision_id="rev-2")
    assert calls == ["called"]
