"""CommanderRuntime generation 生命周期组件测试。

覆盖 Prepare / Activate / Abort 三段式配置事务的核心语义：

- prepare 构造候选 generation，不影响 active；
- activate 原子切换 active generation / revision / config hash；
- activate 与 prepared revision 不匹配时报错且状态不变；
- abort 幂等撤销候选，revision 不匹配返回 False；
- stop 后 prepare/activate 拒绝。

全部为进程内真实对象协作，不做网络 I/O（无 ADS 配置时 activate 的
``start()`` 为空操作）。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from wind_hub_commander.assembly import CommanderApp
from wind_hub_commander.config import load_commander_config
from wind_hub_core.device.session import DeviceSession


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


class TestStop:
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


# ---------------------------------------------------------------------------
# generation 并发生命周期回归：in-flight 操作排空、候选替换、retirement 收敛。
# ---------------------------------------------------------------------------


def _spy_close(sessions: list[DeviceSession]) -> list[str]:
    """给会话安装实例级 close 探针，返回按关闭顺序记录的 device_id 列表。

    组件测试替身：通过实例属性遮蔽类方法记录关闭时机，用于验证
    retirement 排空语义；原始 close 仍被调用，资源语义不变。
    """
    closed: list[str] = []
    for session in sessions:
        original_close = session.close

        async def _close(
            _original: Callable[[], object] = original_close,
            _device_id: str = session.device_id,
        ) -> None:
            closed.append(_device_id)
            await _original()  # type: ignore[misc]  # 探针包装原始协程方法

        # 实例属性遮蔽类方法——探针只作用于注入的会话实例。
        session.close = _close  # type: ignore[method-assign]
    return closed


async def _wait_for(predicate: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """轮询等待条件成立；超时报错（不用硬 sleep 猜测后台收敛时机）。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, "条件在超时内未满足"
        await asyncio.sleep(0.01)


class TestInFlightOperationDrain:
    async def test_activate_waits_for_in_flight_operation(
        self, commander_app: CommanderApp
    ) -> None:
        """activate 原子切换立即生效，但老 generation 在 in-flight 操作
        完成前不得关闭；操作退出后 retirement 必须收敛。"""
        runtime = commander_app.runtime
        old_sessions = list(runtime.devices.values())
        closed = _spy_close(old_sessions)

        async with runtime.operation():  # 固定老 generation 的在途操作
            candidate = load_commander_config(commander_app.config_dir)
            await runtime.prepare_config("rev-2", candidate, "hash-2")
            await runtime.activate_config("rev-2")

            assert runtime.active_revision == "rev-2"
            assert set(runtime.devices) == {s.device_id for s in old_sessions}
            await asyncio.sleep(0.05)  # retirement 已调度但未排空
            assert closed == [], "老 generation 在 in-flight 完成前被关闭"

        await _wait_for(lambda: len(closed) == len(old_sessions))
        assert set(closed) == {s.device_id for s in old_sessions}
        await runtime.stop()  # 收尾：关闭新 generation，避免遗留 retirement 任务

    async def test_stop_waits_for_in_flight_operation(
        self, commander_app: CommanderApp
    ) -> None:
        """stop 必须阻塞等待在途操作排空，且所有 retirement 任务最终收敛。"""
        runtime = commander_app.runtime
        sessions = list(runtime.devices.values())
        closed = _spy_close(sessions)

        async with runtime.operation():
            stop_task = asyncio.create_task(runtime.stop())
            await asyncio.sleep(0.05)
            assert not stop_task.done(), "stop 未等待在途操作排空"
            assert closed == []

        await asyncio.wait_for(stop_task, timeout=5.0)
        assert set(closed) == {s.device_id for s in sessions}
        await asyncio.sleep(0)  # done callback 摘除已完成任务
        assert not runtime._retirement_tasks, "retirement 任务未收敛"

    async def test_rapid_prepare_replacement_closes_previous_candidates(
        self, commander_app: CommanderApp
    ) -> None:
        """连续 prepare 替换候选：被替换的候选同步关闭，不泄漏会话。"""
        runtime = commander_app.runtime
        candidate = load_commander_config(commander_app.config_dir)

        await runtime.prepare_config("rev-2", candidate, "hash-2")
        # prepared generation 未对外暴露——生命周期验证需要引用其会话；
        # 这是白盒组件测试，读取私有字段仅用于断言资源回收。
        replaced = runtime._prepared_generation
        assert replaced is not None
        closed = _spy_close(list(replaced.devices.values()))

        await runtime.prepare_config("rev-3", candidate, "hash-3")
        # 候选替换是同步关闭（不经 retirement 任务）——await 返回即完成。
        assert set(closed) == set(replaced.devices)
        assert runtime.prepared_revision == "rev-3"

        second = runtime._prepared_generation
        assert second is not None
        closed2 = _spy_close(list(second.devices.values()))
        await runtime.prepare_config("rev-4", candidate, "hash-4")
        assert set(closed2) == set(second.devices)
        assert runtime.prepared_revision == "rev-4"

        await runtime.activate_config("rev-4")
        assert runtime.active_revision == "rev-4"
        await runtime.stop()
        await asyncio.sleep(0)
        assert not runtime._retirement_tasks

    async def test_activate_immediately_followed_by_stop(
        self, commander_app: CommanderApp
    ) -> None:
        """activate 后立刻 stop：两代会话全部关闭，无 dangling task。"""
        runtime = commander_app.runtime
        old_sessions = list(runtime.devices.values())
        closed_old = _spy_close(old_sessions)

        candidate = load_commander_config(commander_app.config_dir)
        await runtime.prepare_config("rev-2", candidate, "hash-2")
        await runtime.activate_config("rev-2")
        new_sessions = list(runtime.devices.values())
        closed_new = _spy_close(new_sessions)

        await runtime.stop()

        await _wait_for(lambda: len(closed_old) == len(old_sessions))
        assert set(closed_new) == {s.device_id for s in new_sessions}
        await asyncio.sleep(0)
        assert not runtime._retirement_tasks

        with pytest.raises(RuntimeError, match="stopping"):
            await runtime.prepare_config("rev-3", candidate, "hash-3")
