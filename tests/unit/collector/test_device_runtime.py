"""Collector DeviceRuntime 健康检查、连接状态与热更新单元测试。"""

from __future__ import annotations

import pytest

from collector.application.config import RuntimeParams
from collector.application.device_runtime import DeviceRuntime, is_connection_level
from core.domain.identities import DeviceId
from tests.support.new_collector import (
    CollectorFakeProtocol,
    make_collector_config,
    make_session,
)


def _runtime(
    proto: CollectorFakeProtocol | None = None,
    *,
    clock=None,
) -> tuple[DeviceRuntime, CollectorFakeProtocol]:
    proto = proto or CollectorFakeProtocol()
    config = make_collector_config()
    session = make_session(config, proto)
    runtime = DeviceRuntime(
        {"dev1": session},
        RuntimeParams(connect_timeout=0.2),
        clock=clock or (lambda: 0.0),
    )
    runtime.register_view("dev1", config.device_view("dev1"))  # type: ignore[arg-type]
    return runtime, proto


def test_is_connection_level_walks_cause_chain():
    assert is_connection_level(TimeoutError())
    assert is_connection_level(ConnectionRefusedError())
    wrapped = RuntimeError("protocol failure")
    wrapped.__cause__ = OSError("socket closed")
    assert is_connection_level(wrapped)
    assert not is_connection_level(ValueError("bad data"))


async def test_ensure_connected_fast_path_when_connected():
    runtime, proto = _runtime()
    await runtime.connect_all()
    assert await runtime.ensure_connected("dev1") is True
    assert proto.connect_calls == 1  # 快路径零开销


async def test_normal_acquisition_does_not_retry_connect():
    """采集查询不触发连接，故障恢复由 Watchdog 周期执行。"""
    runtime, proto = _runtime()
    proto.fail_connect = True
    await runtime.connect_all()
    state = runtime.device_state("dev1")
    assert state is not None and not state.connected
    assert state.consecutive_failures == 1

    assert await runtime.ensure_connected("dev1") is False
    assert proto.connect_calls == 1

    proto.fail_connect = False
    await runtime._watchdogs["dev1"].check_once()
    assert proto.connect_calls == 2
    assert await runtime.ensure_connected("dev1") is True
    state = runtime.device_state("dev1")
    assert state is not None and state.connected
    await runtime.close_all()


async def test_explicit_connect_after_initial_failure():
    now = [100.0]
    runtime, proto = _runtime(clock=lambda: now[0])
    proto.fail_connect = True
    await runtime.connect_all()
    proto.fail_connect = False
    assert await runtime.ensure_connected("dev1", force=True) is True


async def test_ensure_connected_unknown_device_false():
    runtime, _ = _runtime()
    assert await runtime.ensure_connected("ghost") is False


async def test_report_read_failure_marks_disconnected_on_connection_level():
    runtime, _ = _runtime()
    await runtime.connect_all()
    runtime.report_read_failure("dev1", OSError("socket closed"))
    state = runtime.device_state("dev1")
    assert state is not None and not state.connected
    assert state.last_error == "socket closed"


async def test_report_read_failure_uses_driver_health_when_not_connection_level():
    runtime, proto = _runtime()
    await runtime.connect_all()
    proto.connected = False  # 驱动自报不健康（库包装了断线但不含 OSError）
    runtime.report_read_failure("dev1", ValueError("weird library error"))
    state = runtime.device_state("dev1")
    assert state is not None and not state.connected


async def test_report_read_success_recovers_connected():
    runtime, _ = _runtime()
    runtime.report_read_failure("dev1", OSError("down"))
    runtime.report_read_success("dev1")
    state = runtime.device_state("dev1")
    assert state is not None and state.connected and state.last_error is None


async def test_add_and_remove_device():
    runtime, _ = _runtime()
    config = make_collector_config(device_id="dev2")
    proto2 = CollectorFakeProtocol()
    session2 = make_session(config, proto2, device_id="dev2")
    await runtime.add_device("dev2", config.device_view("dev2"), session2)  # type: ignore[arg-type]
    assert "dev2" in runtime.devices and proto2.connect_calls == 1

    await runtime.remove_device("dev2")
    assert "dev2" not in runtime.devices
    assert runtime.device_state("dev2") is None
    assert proto2.close_calls == 1


async def test_rebuild_device_closes_old_and_resets_state():
    runtime, proto = _runtime()
    await runtime.connect_all()
    config = make_collector_config()
    proto2 = CollectorFakeProtocol()
    session2 = make_session(config, proto2)
    await runtime.rebuild_device("dev1", config.device_view("dev1"), session2)  # type: ignore[arg-type]
    assert proto.close_calls == 1
    assert proto2.connect_calls == 1
    state = runtime.device_state("dev1")
    assert state is not None and state.connected and state.consecutive_failures == 0


def test_lightweight_update_only_table_or_group_change():
    runtime, _ = _runtime()
    config = make_collector_config()
    same = config.device_view("dev1")  # type: ignore[arg-type]
    assert runtime.is_lightweight_update("dev1", same)

    # endpoint 变化 → 非轻量
    other = make_collector_config()
    view = other.device_view("dev1")  # type: ignore[arg-type]
    from dataclasses import replace

    changed = replace(
        view, device=replace(view.device, endpoint=replace(view.device.endpoint, port=503))
    )
    assert not runtime.is_lightweight_update("dev1", changed)
    assert runtime.requires_rebuild("dev1", changed)
    assert not runtime.requires_rebuild("dev1", same)


async def test_close_all_bounds_hung_device_close():
    """单台关闭挂起以 connect_timeout 为上界，不阻断其余设备释放。"""
    import asyncio

    hung = CollectorFakeProtocol()
    healthy = CollectorFakeProtocol()

    async def blocked_close() -> None:
        await asyncio.Event().wait()

    hung.close = blocked_close  # type: ignore[method-assign]  # 测试替身：确定性替换实例方法
    config1 = make_collector_config(device_id="dev1")
    config2 = make_collector_config(device_id="dev2")
    runtime = DeviceRuntime(
        {
            "dev1": make_session(config1, hung),
            "dev2": make_session(config2, healthy, device_id="dev2"),
        },
        RuntimeParams(connect_timeout=0.05),
    )

    await runtime.close_all()

    assert healthy.close_calls == 1


async def test_watchdog_reconnect_marks_state_and_metric():
    """读路径不再透明重连，Watchdog 成功恢复后更新状态和指标。"""
    events: list[tuple[str, str]] = []

    class _Metrics:
        def device_reconnected(self, device_id: str, protocol: str) -> None:
            events.append((device_id, protocol))

    proto = CollectorFakeProtocol()
    config = make_collector_config()
    session = make_session(config, proto)
    runtime = DeviceRuntime(
        {"dev1": session},
        RuntimeParams(connect_timeout=0.2),
        clock=lambda: 0.0,
        metrics_hook=_Metrics(),  # type: ignore[arg-type]
    )
    await runtime.connect_all()
    proto.connected = False
    await runtime._watchdogs["dev1"].check_once()
    assert events == [("dev1", "modbus")]
    assert proto.connect_calls == 2
    assert runtime.device_state("dev1").connected  # type: ignore[union-attr]
    await runtime.close_all()


class _RecordingMetrics:
    def __init__(self) -> None:
        self.reconnected: list[tuple[str, str]] = []
        self.connect_failed: list[tuple[str, str]] = []

    def device_reconnected(self, device_id: str, protocol: str) -> None:
        self.reconnected.append((device_id, protocol))

    def device_connect_failed(self, device_id: str, protocol: str) -> None:
        self.connect_failed.append((device_id, protocol))


async def test_watchdog_failed_connect_is_recorded_and_recovers():
    """周期重连失败需要记录一次设备失败；恢复后清零连续失败。"""
    metrics = _RecordingMetrics()
    config = make_collector_config()
    proto = CollectorFakeProtocol()
    session = make_session(config, proto)
    runtime = DeviceRuntime(
        {"dev1": session},
        RuntimeParams(connect_timeout=0.2),
        clock=lambda: 1.0,
        metrics_hook=metrics,  # type: ignore[arg-type]
    )
    await runtime.connect_all()

    proto.connected = False
    proto.fail_connect = True
    with pytest.raises(ConnectionError):
        await runtime._watchdogs["dev1"].check_once()

    state = runtime.device_state("dev1")
    assert state is not None
    assert state.consecutive_failures == 1
    assert metrics.connect_failed == [("dev1", "modbus")]

    proto.fail_connect = False
    await runtime._watchdogs["dev1"].check_once()
    assert state.connected
    assert state.consecutive_failures == 0
    assert metrics.reconnected == [("dev1", "modbus")]
    await runtime.close_all()


async def test_rebuild_stops_old_watchdog_and_isolates_state():
    metrics = _RecordingMetrics()
    config = make_collector_config()
    old_proto = CollectorFakeProtocol()
    old_session = make_session(config, old_proto)
    runtime = DeviceRuntime(
        {"dev1": old_session},
        RuntimeParams(connect_timeout=0.2),
        clock=lambda: 0.0,
        metrics_hook=metrics,  # type: ignore[arg-type]
    )
    await runtime.connect_all()
    old_watchdog = runtime._watchdogs["dev1"]
    new_proto = CollectorFakeProtocol()
    new_session = make_session(config, new_proto)
    await runtime.rebuild_device("dev1", config.device_view(DeviceId("dev1")), new_session)
    assert old_watchdog._task is None
    assert runtime.devices["dev1"] is new_session
    assert metrics.reconnected == []
    await runtime.close_all()


async def test_removed_device_watchdog_cannot_reconnect():
    metrics = _RecordingMetrics()
    config = make_collector_config()
    proto = CollectorFakeProtocol()
    session = make_session(config, proto)
    runtime = DeviceRuntime(
        {"dev1": session},
        RuntimeParams(connect_timeout=0.2),
        clock=lambda: 0.0,
        metrics_hook=metrics,  # type: ignore[arg-type]
    )
    await runtime.connect_all()
    watchdog = runtime._watchdogs["dev1"]
    await runtime.remove_device("dev1")
    proto.connected = False
    await watchdog.check_once()
    assert proto.connect_calls == 1
    assert runtime.device_state("dev1") is None


async def test_concurrent_ensure_connected_single_connect_and_metric():
    """并发 ensure_connected 在同一断线事件上串行：只 connect 一次、只记账一次。"""
    import asyncio

    metrics = _RecordingMetrics()
    proto = CollectorFakeProtocol()
    config = make_collector_config()
    session = make_session(config, proto)
    runtime = DeviceRuntime(
        {"dev1": session},
        RuntimeParams(connect_timeout=0.5),
        clock=lambda: 0.0,
        metrics_hook=metrics,  # type: ignore[arg-type]
    )

    connect_started = asyncio.Event()

    async def slow_connect() -> None:
        proto.connect_calls += 1
        connect_started.set()
        await asyncio.sleep(0.02)
        proto.connected = True

    proto.connect = slow_connect  # type: ignore[method-assign]

    results = await asyncio.gather(
        *(runtime.ensure_connected("dev1", force=True) for _ in range(8))
    )
    assert all(results)
    assert proto.connect_calls == 1
    assert metrics.reconnected == [("dev1", "modbus")]


async def test_ensure_connected_aborts_when_device_rebuilt_while_awaiting_lock():
    """等锁期间设备被重建：旧调用不再触碰新会话，直接返回 False。"""
    import asyncio

    proto = CollectorFakeProtocol()
    config = make_collector_config()
    session = make_session(config, proto)
    runtime = DeviceRuntime(
        {"dev1": session},
        RuntimeParams(connect_timeout=0.5),
        clock=lambda: 0.0,
    )

    connect_started = asyncio.Event()
    release_connect = asyncio.Event()

    async def slow_connect() -> None:
        proto.connect_calls += 1
        connect_started.set()
        await release_connect.wait()
        proto.connected = True

    proto.connect = slow_connect  # type: ignore[method-assign]

    first = asyncio.create_task(runtime.ensure_connected("dev1", force=True))
    await connect_started.wait()

    second = asyncio.create_task(runtime.ensure_connected("dev1", force=True))
    await asyncio.sleep(0)  # second 进入锁等待

    new_proto = CollectorFakeProtocol()
    new_session = make_session(config, new_proto)
    # 重建会关闭旧会话（proto.connected=False），换入新会话。
    rebuild = asyncio.create_task(
        runtime.rebuild_device("dev1", config.device_view("dev1"), new_session)  # type: ignore[arg-type]
    )
    await asyncio.sleep(0)
    release_connect.set()
    await asyncio.gather(first, rebuild)
    assert await second is False
    assert runtime.devices["dev1"] is new_session
