"""Watchdog 重连成功后的采集任务恢复链（第五轮验收专项）。

完整执行链：WatchdogUseCase.on_success → DeviceRuntime._restore_push →
CollectorRuntime.restore_device_tasks（冷启动/停机闸门）→
TaskRuntime.start_device_tasks（逐实例锁 + can_restore 重检）。

契约：
- Collector 冷启动不自动启动采集；
- 重连成功启动该设备全部已启用实例（POLL/SUBSCRIBE/LISTEN 同一入口）；
- 禁用任务不展开、不启动；重连失败不启动；重复恢复不产生重复句柄；
- Collector 停机/冷启动未完成期间恢复被闸门拒绝；
- 故障设备重连不影响健康设备。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence
from typing import Any

import pytest

from collector.application.config import CollectionTask, RuntimeParams
from collector.application.runtime import CollectorRuntime
from collector.application.session import AcquisitionMode
from collector.application.task_instance import TaskInstanceState
from collector.domain.acquisition import AcquisitionEngine
from core.application import ProtocolCapability
from core.domain import DeviceGroupId
from tests.support.new_collector import (
    CollectorFakeProtocol,
    make_collector_config,
    make_runtime,
    make_session,
)
from tests.support.new_commander import FakeSubscriptionHandle

pytestmark = pytest.mark.component

POLL_CAPS = frozenset({ProtocolCapability.READ, ProtocolCapability.WRITE})


def _task(task_id: str, group: str, *, interval: float = 0.02, enabled: bool = True):
    return CollectionTask(
        task_id=task_id,
        device_group_id=DeviceGroupId(group),
        point_group="g",
        interval=interval,
        sink_ids=("s1",),
        enabled=enabled,
    )


def _watchdog(runtime: CollectorRuntime, device_id: str = "dev1"):
    return runtime.device_runtime._watchdogs[device_id]


def _handles(runtime: CollectorRuntime) -> dict:
    return runtime.task_runtime._acquisition_handles


async def _restore(runtime: CollectorRuntime, device_id: str = "dev1") -> None:
    """模拟 Watchdog 重连成功：经真实 watchdog.reconnect 触发 on_success。"""
    await _watchdog(runtime, device_id).reconnect()


def _subscribe_runtime(config, proto: CollectorFakeProtocol) -> CollectorRuntime:
    session = make_session(config, proto)
    return CollectorRuntime(
        devices={"dev1": session},
        sinks={},
        engine=AcquisitionEngine(),
        params=config.runtime,
        tasks=dict(config.tasks),
    )


def _record_subscription_handles(
    proto: CollectorFakeProtocol,
) -> list[FakeSubscriptionHandle]:
    created: list[FakeSubscriptionHandle] = []
    original = proto.subscribe

    def wrapped(point_ids: Sequence[str], callback: Any, *, interval: float | None = None):
        handle = original(point_ids, callback, interval=interval)
        created.append(handle)
        return handle

    proto.subscribe = wrapped  # type: ignore[method-assign]
    return created


async def test_cold_start_registers_instances_but_starts_no_acquisition() -> None:
    config = make_collector_config(tasks={"t1": _task("t1", "dev1")})
    runtime, proto = make_runtime(config)
    await runtime.start()
    try:
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED
        assert _handles(runtime) == {}
        assert proto.subscriptions == []
        assert proto.listeners == []
    finally:
        await runtime.stop()


async def test_watchdog_reconnect_restores_running_poll_tasks() -> None:
    config = make_collector_config(
        tasks={
            "t1": _task("t1", "dev1", interval=0.02),
            "t2": _task("t2", "dev1", interval=0.02),
        }
    )
    runtime, proto = make_runtime(config)
    await runtime.start()
    try:
        await runtime.start_task_instance("t1:dev1")
        collected_before = runtime.points_collected
        proto.connected = False
        await _restore(runtime)
        states = runtime.instance_states()
        # 断线前 RUNNING 的实例必须恢复；POLL 句柄真实跑起来。
        assert states["t1:dev1"] is TaskInstanceState.RUNNING
        assert "t1:dev1" in _handles(runtime)
        await asyncio.sleep(0.07)
        assert runtime.points_collected > collected_before
    finally:
        await runtime.stop()


async def test_never_started_enabled_task_stopped_at_cold_boot_running_after_reconnect() -> None:
    """方案 A：从未启动但 enabled 的实例冷启动保持 STOPPED，Watchdog 重连后 RUNNING。

    第七轮契约修正（原 test_never_started_task_stays_stopped_after_reconnect
    与方案 A 不一致——重连成功本就应启动故障设备的全部已启用实例）。
    """
    config = make_collector_config(tasks={"t1": _task("t1", "dev1")})
    runtime, proto = make_runtime(config)
    await runtime.start()
    try:
        # Collector 冷启动：enabled 但从未启动的实例只注册、不建句柄。
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED
        assert _handles(runtime) == {}
        # Watchdog 重连成功：方案 A 自动启动该设备全部已启用实例。
        proto.connected = False
        await _restore(runtime)
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
        assert "t1:dev1" in _handles(runtime)
    finally:
        await runtime.stop()


async def test_task_stopped_during_outage_stays_stopped_after_recovery() -> None:
    config = make_collector_config(
        tasks={
            "t1": _task("t1", "dev1", interval=0.02),
            "t2": _task("t2", "dev1", interval=0.02),
        }
    )
    runtime, proto = make_runtime(config)
    await runtime.start()
    try:
        await runtime.start_task_instance("t1:dev1")
        await runtime.start_task_instance("t2:dev1")
        proto.connected = False
        # 设备不可用期间操作员显式停止 t1。
        await runtime.stop_task_instance("t1:dev1")
        await _restore(runtime)
        states = runtime.instance_states()
        assert states["t1:dev1"] is TaskInstanceState.STOPPED
        assert "t1:dev1" not in _handles(runtime)
        # 未被停止的 t2 正常恢复。
        assert states["t2:dev1"] is TaskInstanceState.RUNNING
    finally:
        await runtime.stop()


async def test_stop_after_disconnect_bookkeeping_persists_across_recoveries() -> None:
    """第六轮：采集失败已把设备状态簿记切到 disconnected 后显式 stop。

    锁定 a105326a 修复的时序窗——停止意图必须跨多次 Watchdog 重连持续
    有效，用户手动启动后才解除限制（方案 A）。
    """
    config = make_collector_config(tasks={"t1": _task("t1", "dev1", interval=0.02)})
    runtime, proto = make_runtime(config)
    await runtime.start()
    try:
        await runtime.start_task_instance("t1:dev1")
        proto.connected = False
        # 与真实引擎路径一致：连接级采集失败先把簿记状态切到 disconnected。
        runtime.device_runtime.report_read_failure("dev1", ConnectionError("connection reset"))
        assert runtime.device_state("dev1") is not None
        assert runtime.device_state("dev1").connected is False
        await runtime.stop_task_instance("t1:dev1")

        await _restore(runtime)
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED
        assert "t1:dev1" not in _handles(runtime)

        # 停止意图跨多次重连持续有效。
        await _restore(runtime)
        await _restore(runtime)
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED
        assert "t1:dev1" not in _handles(runtime)

        # 用户再次手动启动：解除停止限制，后续重连正常恢复。
        await runtime.start_task_instance("t1:dev1")
        proto.connected = False
        runtime.device_runtime.report_read_failure("dev1", ConnectionError("connection reset"))
        await _restore(runtime)
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
        assert "t1:dev1" in _handles(runtime)
    finally:
        await runtime.stop()


async def test_stop_intent_does_not_leak_into_same_named_recreated_task() -> None:
    """第七轮 D6-b：旧实例的停止意图不得作用于删除后同名重建的新实例。

    实例注销（sync_instances）必须丢弃 _recovery_stopped 簿记；重连后
    新实例按方案 A 作为"从未启动的 enabled 任务"自动启动。
    """
    from collector.application.reload import compute_diff

    config = make_collector_config(tasks={"t1": _task("t1", "dev1", interval=0.02)})
    runtime, proto = make_runtime(config)
    await runtime.start()
    try:
        await runtime.start_task_instance("t1:dev1")
        proto.connected = False
        await runtime.stop_task_instance("t1:dev1")
        assert "t1:dev1" in runtime.task_runtime._recovery_stopped

        # 离线期间删除 t1，再以同名重建（新实例代际）。
        empty = make_collector_config(tasks={})
        await runtime.reconfigure(empty, compute_diff(config, empty))
        assert "t1:dev1" not in runtime.task_instances()
        assert "t1:dev1" not in runtime.task_runtime._recovery_stopped
        reborn = make_collector_config(tasks={"t1": _task("t1", "dev1", interval=0.02)})
        await runtime.reconfigure(reborn, compute_diff(empty, reborn))
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED

        await _restore(runtime)
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
        assert "t1:dev1" in _handles(runtime)
    finally:
        await runtime.stop()


async def test_stop_intent_does_not_leak_into_disabled_then_reenabled_task() -> None:
    """第七轮 D6-b：禁用再启用后的同名实例不继承禁用前的停止意图。"""
    from collector.application.reload import compute_diff

    config = make_collector_config(tasks={"t1": _task("t1", "dev1", interval=0.02)})
    runtime, proto = make_runtime(config)
    await runtime.start()
    try:
        await runtime.start_task_instance("t1:dev1")
        proto.connected = False
        await runtime.stop_task_instance("t1:dev1")
        assert "t1:dev1" in runtime.task_runtime._recovery_stopped

        disabled = make_collector_config(
            tasks={"t1": _task("t1", "dev1", interval=0.02, enabled=False)}
        )
        await runtime.reconfigure(disabled, compute_diff(config, disabled))
        assert "t1:dev1" not in runtime.task_instances()
        reenabled = make_collector_config(
            tasks={"t1": _task("t1", "dev1", interval=0.02, enabled=True)}
        )
        await runtime.reconfigure(reenabled, compute_diff(disabled, reenabled))
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED

        await _restore(runtime)
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
        assert "t1:dev1" in _handles(runtime)
    finally:
        await runtime.stop()


async def test_watchdog_reconnect_restores_subscribe_mode() -> None:
    config = make_collector_config(
        protocol="ads", ads_subscribe=True, tasks={"t1": _task("t1", "dev1")}
    )
    proto = CollectorFakeProtocol()
    proto.capabilities_value = frozenset({*POLL_CAPS, ProtocolCapability.SUBSCRIBE})
    runtime = _subscribe_runtime(config, proto)
    assert runtime.devices["dev1"].acquisition_mode is AcquisitionMode.SUBSCRIBE
    await runtime.start()
    try:
        await runtime.start_task_instance("t1:dev1")
        assert len(proto.subscriptions) == 1
        proto.connected = False
        await _restore(runtime)
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
        # 重连后在新会话上重新注册原生订阅（旧句柄已关闭，不重复累积）。
        assert len(proto.subscriptions) == 2
        assert "t1:dev1" in _handles(runtime)
    finally:
        await runtime.stop()


async def test_watchdog_reconnect_restores_listen_mode_with_interrogation() -> None:
    config = make_collector_config(protocol="iec104", tasks={"t1": _task("t1", "dev1")})
    proto = CollectorFakeProtocol()
    proto.capabilities_value = frozenset({*POLL_CAPS, ProtocolCapability.LISTEN})
    session = make_session(config, proto)
    runtime = CollectorRuntime(
        devices={"dev1": session},
        sinks={},
        engine=AcquisitionEngine(),
        params=config.runtime,
        tasks=dict(config.tasks),
    )
    assert session.acquisition_mode is AcquisitionMode.LISTEN
    await runtime.start()
    try:
        await runtime.start_task_instance("t1:dev1")
        proto.connected = False
        await _restore(runtime)
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
        # 重连后重建监听并再次显式总召。
        assert len(proto.listeners) == 2
        assert len(proto.interrogate_calls) == 2
    finally:
        await runtime.stop()


async def test_disabled_task_is_not_expanded_or_started_on_restore() -> None:
    config = make_collector_config(
        tasks={
            "t1": _task("t1", "dev1"),
            "t2": _task("t2", "dev1", enabled=False),
        }
    )
    runtime, _ = make_runtime(config)
    await runtime.start()
    try:
        await runtime.start_task_instance("t1:dev1")
        await _restore(runtime)
        # enabled:false 任务不展开，恢复时也绝不出现。
        assert "t2:dev1" not in runtime.task_instances()
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
    finally:
        await runtime.stop()


async def test_failed_reconnect_does_not_start_or_replace_handles() -> None:
    config = make_collector_config(tasks={"t1": _task("t1", "dev1")})
    runtime, proto = make_runtime(config)
    await runtime.start()
    await runtime.start_task_instance("t1:dev1")
    handle_before = _handles(runtime)["t1:dev1"]
    try:
        proto.connected = False
        proto.fail_connect = True
        with pytest.raises(ConnectionError, match="fake connect failure"):
            await _watchdog(runtime).reconnect()
        # on_success 不得执行：无新订阅、无句柄替换。
        assert proto.subscriptions == []
        assert _handles(runtime)["t1:dev1"] is handle_before
        assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.RUNNING
    finally:
        proto.fail_connect = False
        proto.connected = True
        await runtime.stop()


async def test_repeated_recovery_keeps_single_live_handle_per_instance() -> None:
    config = make_collector_config(
        protocol="ads", ads_subscribe=True, tasks={"t1": _task("t1", "dev1")}
    )
    proto = CollectorFakeProtocol()
    proto.capabilities_value = frozenset({*POLL_CAPS, ProtocolCapability.SUBSCRIBE})
    created = _record_subscription_handles(proto)
    runtime = _subscribe_runtime(config, proto)
    await runtime.start()
    try:
        await runtime.start_task_instance("t1:dev1")
        await _restore(runtime)
        await _restore(runtime)
        await _restore(runtime)
        # 初始 1 次 + 三次恢复重新注册：任意时刻只允许一个活动句柄，
        # 旧句柄必须已 stop（不产生重复采集/泄漏）。
        assert len(proto.subscriptions) == 4
        assert len(_handles(runtime)) == 1
        assert [h.stop_calls for h in created] == [1, 1, 1, 0]
        assert created[-1].started and not created[-1].closed
    finally:
        await runtime.stop()
    assert created[-1].stop_calls == 1


async def test_manual_start_stop_racing_recovery_leaves_no_duplicate_handle() -> None:
    config = make_collector_config(tasks={"t1": _task("t1", "dev1", interval=0.02)})
    runtime, _ = make_runtime(config)
    await runtime.start()
    try:

        async def hammer_manual() -> None:
            for _ in range(10):
                with contextlib.suppress(Exception):
                    await runtime.start_task_instance("t1:dev1")
                await asyncio.sleep(0)
                with contextlib.suppress(Exception):
                    await runtime.stop_task_instance("t1:dev1")

        async def hammer_restore() -> None:
            for _ in range(10):
                await _restore(runtime)
                await asyncio.sleep(0)

        await asyncio.gather(hammer_manual(), hammer_restore())
        assert len(_handles(runtime)) <= 1
        # 末态必须可收敛：再停一次不报错，句柄清零。
        await runtime.stop_task_instance("t1:dev1")
        assert _handles(runtime) == {}
    finally:
        await runtime.stop()


async def test_restore_rejected_after_collector_shutdown() -> None:
    config = make_collector_config(tasks={"t1": _task("t1", "dev1")})
    runtime, _ = make_runtime(config)
    await runtime.start()
    callback = runtime.device_runtime._reconnect_callback
    assert callback is not None
    await runtime.stop()
    # 停机后迟到的 watchdog on_success 必须被闸门吞掉，不留下任何句柄。
    await callback("dev1")
    assert runtime.instance_states()["t1:dev1"] is TaskInstanceState.STOPPED
    assert _handles(runtime) == {}


async def test_hot_reload_racing_recovery_leaves_no_duplicate_or_orphan_handle() -> None:
    from collector.application.reload import compute_diff

    config = make_collector_config(tasks={"t1": _task("t1", "dev1", interval=0.02)})
    runtime, proto = make_runtime(config)
    await runtime.start()
    try:

        async def hammer_reload() -> None:
            current = config
            for index in range(10):
                new_config = make_collector_config(
                    tasks={"t1": _task("t1", "dev1", interval=0.02 + index * 0.001)}
                )
                diff = compute_diff(current, new_config)
                errors = await runtime.reconfigure(new_config, diff)
                assert errors == []
                current = new_config
                await asyncio.sleep(0)

        async def hammer_restore() -> None:
            for _ in range(10):
                proto.connected = False
                await _restore(runtime)
                await asyncio.sleep(0)

        await asyncio.gather(hammer_reload(), hammer_restore())
        assert len(_handles(runtime)) <= 1
        await runtime.stop_task_instance("t1:dev1")
        assert _handles(runtime) == {}
        # 热更新后恢复仍可正常启停。
        await runtime.start_task_instance("t1:dev1")
        assert "t1:dev1" in _handles(runtime)
        await runtime.stop_task_instance("t1:dev1")
        assert _handles(runtime) == {}
    finally:
        await runtime.stop()


async def test_can_restore_gate_aborts_between_instances() -> None:
    config = make_collector_config(
        tasks={
            "t1": _task("t1", "dev1"),
            "t2": _task("t2", "dev1"),
        }
    )
    runtime, _ = make_runtime(config)
    await runtime.start()
    try:
        await runtime.start_task_instance("t1:dev1")
        await runtime.start_task_instance("t2:dev1")
        t2_handle = _handles(runtime)["t2:dev1"]
        calls = 0

        def can_restore() -> bool:
            nonlocal calls
            calls += 1
            # t1 恢复完成后的下一次检查时 Collector 进入停机。
            return calls <= 3

        await runtime.task_runtime.start_device_tasks("dev1", can_restore=can_restore)
        # 未进入恢复区间的 t2 必须原样保留（RUNNING + 原句柄），
        # 不允许“先停后检”留下半停实例。
        assert runtime.instance_states()["t2:dev1"] is TaskInstanceState.RUNNING
        assert _handles(runtime)["t2:dev1"] is t2_handle
    finally:
        await runtime.stop()


async def test_faulty_device_reconnect_does_not_touch_healthy_device() -> None:
    tasks1 = {"t1": _task("t1", "dev1")}
    tasks2 = {"t2": _task("t2", "dev2")}
    c1 = make_collector_config(device_id="dev1", tasks=tasks1)
    c2 = make_collector_config(device_id="dev2", tasks=tasks2)
    p1, p2 = CollectorFakeProtocol(), CollectorFakeProtocol()
    runtime = CollectorRuntime(
        devices={
            "dev1": make_session(c1, p1, device_id="dev1"),
            "dev2": make_session(c2, p2, device_id="dev2"),
        },
        sinks={},
        engine=AcquisitionEngine(),
        params=RuntimeParams(connect_timeout=0.2, shutdown_timeout=0.5),
        tasks={**c1.tasks, **c2.tasks},
    )
    await runtime.start()
    await runtime.start_task_instance("t2:dev2")
    healthy_handle = _handles(runtime)["t2:dev2"]
    try:
        p1.connected = False
        p1.fail_connect = True
        with pytest.raises(ConnectionError):
            await _watchdog(runtime, "dev1").reconnect()
        # 健康设备：连接不触碰、句柄不替换、状态仍 RUNNING。
        assert p2.connect_calls == 1
        assert p2.close_calls == 0
        assert _handles(runtime)["t2:dev2"] is healthy_handle
        assert runtime.instance_states()["t2:dev2"] is TaskInstanceState.RUNNING
    finally:
        p1.fail_connect = False
        p1.connected = True
        await runtime.stop()
