"""新 CommanderRuntime generation 生命周期与配置事务单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from commander.application.runtime import CommanderRuntime
from tests.support.new_commander import FakeRegistry, make_commander_config


def _runtime(registry: FakeRegistry | None = None) -> tuple[CommanderRuntime, FakeRegistry]:
    reg = registry or FakeRegistry()
    return (
        CommanderRuntime(
            make_commander_config(),
            config_hash="hash-a",
            protocol_registry=reg,  # type: ignore[arg-type]
        ),
        reg,
    )


async def test_prepare_activate_switches_generation_and_retires_old():
    runtime, registry = _runtime()
    old_session = runtime.devices["dev1"]
    old_protocol = registry.instances[0]

    await runtime.prepare_config("r2", make_commander_config(), "hash-b")
    assert runtime.prepared_revision == "r2"
    assert runtime.active_revision == "startup"

    await runtime.activate_config("r2")
    assert runtime.active_revision == "r2"
    assert runtime.active_config_hash == "hash-b"
    assert runtime.prepared_revision is None
    # 新 generation 使用新会话；旧会话在排空后关闭
    assert runtime.devices["dev1"] is not old_session
    await asyncio.sleep(0.05)
    assert old_protocol.close_calls == 1


async def test_activate_waits_for_inflight_operation_before_retiring():
    runtime, registry = _runtime()
    old_protocol = registry.instances[0]

    async with runtime.operation():
        await runtime.prepare_config("r2", make_commander_config(), "hash-b")
        await runtime.activate_config("r2")
        await asyncio.sleep(0.05)
        # 在途操作未结束：旧 generation 不得回收
        assert old_protocol.close_calls == 0
        # 在途操作仍固定旧 generation 配置
        assert runtime.operation_config() is not runtime.config

    await asyncio.sleep(0.05)
    assert old_protocol.close_calls == 1


async def test_prepare_replaces_previous_candidate_and_closes_it():
    runtime, registry = _runtime()
    await runtime.prepare_config("r2", make_commander_config(), "hash-b")
    await runtime.prepare_config("r3", make_commander_config(), "hash-c")
    assert runtime.prepared_revision == "r3"
    # 被替换的候选会话已同步关闭（registry.instances[1] 是第一个候选）
    assert registry.instances[1].close_calls == 1

    await runtime.activate_config("r3")
    assert runtime.active_revision == "r3"


async def test_abort_is_idempotent_and_keeps_active_generation():
    runtime, _ = _runtime()
    active_session = runtime.devices["dev1"]
    await runtime.prepare_config("r2", make_commander_config(), "hash-b")

    assert await runtime.abort_config("r2") is True
    assert await runtime.abort_config("r2") is False
    assert await runtime.abort_config("other") is False
    assert runtime.prepared_revision is None
    assert runtime.devices["dev1"] is active_session
    assert runtime.active_revision == "startup"


async def test_activate_revision_mismatch_rejected():
    runtime, _ = _runtime()
    await runtime.prepare_config("r2", make_commander_config(), "hash-b")
    with pytest.raises(ValueError, match="prepared revision mismatch"):
        await runtime.activate_config("rX")
    assert runtime.active_revision == "startup"


async def test_prepare_rejects_ads_identity_change():
    from tests.support.new_commander import ADSLocalIdentity  # noqa: PLC0415

    runtime, _ = _runtime()
    candidate = make_commander_config(
        ads_local=ADSLocalIdentity(local_ams_net_id="1.2.3.4.1.1", local_ip="127.0.0.1")
    )
    with pytest.raises(ValueError, match="ADS local identity"):
        await runtime.prepare_config("r2", candidate, "hash-b")
    assert runtime.prepared_revision is None


async def test_stop_retires_active_and_prepared_sessions():
    runtime, registry = _runtime()
    await runtime.prepare_config("r2", make_commander_config(), "hash-b")
    await runtime.stop()
    # active + prepared 全部关闭
    assert all(instance.close_calls == 1 for instance in registry.instances)
    with pytest.raises(RuntimeError, match="stopping"):
        await runtime.prepare_config("r3", make_commander_config(), "hash-c")
