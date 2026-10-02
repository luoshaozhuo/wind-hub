"""CommanderRuntime generation 生命周期组件测试。

覆盖 Prepare / Activate / Abort 三段式配置事务的核心语义：

- prepare 构造候选 generation，不影响 active；
- activate 原子切换 active generation / revision / config hash；
- activate 与 prepared revision 不匹配时报错且状态不变；
- abort 幂等撤销候选，revision 不匹配返回 False；
- reload 兼容路径等价于 prepare + activate；
- stop 后 prepare/activate 拒绝。

全部为进程内真实对象协作，不做网络 I/O（无 ADS 配置时 activate 的
``start()`` 为空操作）。
"""

from __future__ import annotations

import pytest

from wind_hub_commander.assembly import CommanderApp
from wind_hub_commander.config import load_commander_config


class TestPrepareActivate:
    async def test_prepare_does_not_change_active(self, commander_app: CommanderApp) -> None:
        runtime = commander_app.runtime
        active_before = runtime.active_config_hash
        candidate = load_commander_config(commander_app.config_dir)

        await runtime.prepare_config("rev-2", candidate, "hash-2")

        assert runtime.active_config_hash == active_before
        assert runtime.active_revision == "startup"
        assert runtime.prepared_revision == "rev-2"
        assert runtime.prepared_config_hash == "hash-2"

    async def test_activate_switches_generation_atomically(
        self, commander_app: CommanderApp
    ) -> None:
        runtime = commander_app.runtime
        old_devices = dict(runtime.devices)
        candidate = load_commander_config(commander_app.config_dir)
        await runtime.prepare_config("rev-2", candidate, "hash-2")

        await runtime.activate_config("rev-2")

        assert runtime.active_revision == "rev-2"
        assert runtime.active_config_hash == "hash-2"
        assert runtime.prepared_revision is None
        assert runtime.prepared_config_hash is None
        # generation 切换重建会话注册表——同 device_id 但不同会话对象。
        assert set(runtime.devices) == set(old_devices)
        assert all(
            runtime.devices[device_id] is not old
            for device_id, old in old_devices.items()
        )

    async def test_activate_with_mismatched_revision_fails_and_preserves_state(
        self, commander_app: CommanderApp
    ) -> None:
        runtime = commander_app.runtime
        candidate = load_commander_config(commander_app.config_dir)
        await runtime.prepare_config("rev-2", candidate, "hash-2")

        with pytest.raises(ValueError, match="prepared revision mismatch"):
            await runtime.activate_config("rev-other")

        assert runtime.active_revision == "startup"
        assert runtime.prepared_revision == "rev-2"

    async def test_activate_without_prepare_fails(self, commander_app: CommanderApp) -> None:
        with pytest.raises(ValueError, match="prepared revision mismatch"):
            await commander_app.runtime.activate_config("rev-x")

    async def test_prepare_replaces_previous_candidate(
        self, commander_app: CommanderApp
    ) -> None:
        runtime = commander_app.runtime
        candidate = load_commander_config(commander_app.config_dir)
        await runtime.prepare_config("rev-2", candidate, "hash-2")
        await runtime.prepare_config("rev-3", candidate, "hash-3")

        assert runtime.prepared_revision == "rev-3"
        await runtime.activate_config("rev-3")
        assert runtime.active_revision == "rev-3"

    async def test_prepare_rejects_empty_identifiers(
        self, commander_app: CommanderApp
    ) -> None:
        runtime = commander_app.runtime
        candidate = load_commander_config(commander_app.config_dir)
        with pytest.raises(ValueError, match="revision_id"):
            await runtime.prepare_config("", candidate, "hash")
        with pytest.raises(ValueError, match="config_hash"):
            await runtime.prepare_config("rev", candidate, "")


class TestAbort:
    async def test_abort_discards_candidate(self, commander_app: CommanderApp) -> None:
        runtime = commander_app.runtime
        candidate = load_commander_config(commander_app.config_dir)
        await runtime.prepare_config("rev-2", candidate, "hash-2")

        aborted = await runtime.abort_config("rev-2")

        assert aborted is True
        assert runtime.prepared_revision is None
        assert runtime.active_revision == "startup"

    async def test_abort_with_mismatched_revision_returns_false(
        self, commander_app: CommanderApp
    ) -> None:
        runtime = commander_app.runtime
        candidate = load_commander_config(commander_app.config_dir)
        await runtime.prepare_config("rev-2", candidate, "hash-2")

        assert await runtime.abort_config("rev-other") is False
        assert runtime.prepared_revision == "rev-2"

    async def test_abort_without_candidate_returns_false(
        self, commander_app: CommanderApp
    ) -> None:
        assert await commander_app.runtime.abort_config("rev-x") is False


class TestReloadAndStop:
    async def test_reload_is_prepare_plus_activate(self, commander_app: CommanderApp) -> None:
        runtime = commander_app.runtime
        candidate = load_commander_config(commander_app.config_dir)

        await runtime.reload(candidate, config_hash="hash-r", revision_id="rev-r")

        assert runtime.active_revision == "rev-r"
        assert runtime.active_config_hash == "hash-r"

    async def test_prepare_after_stop_is_rejected(self, commander_app: CommanderApp) -> None:
        runtime = commander_app.runtime
        candidate = load_commander_config(commander_app.config_dir)
        await runtime.stop()

        with pytest.raises(RuntimeError, match="stopping"):
            await runtime.prepare_config("rev-2", candidate, "hash-2")

    async def test_activate_after_stop_is_rejected(self, commander_app: CommanderApp) -> None:
        runtime = commander_app.runtime
        candidate = load_commander_config(commander_app.config_dir)
        await runtime.prepare_config("rev-2", candidate, "hash-2")
        await runtime.stop()

        with pytest.raises(RuntimeError, match="stopping"):
            await runtime.activate_config("rev-2")
