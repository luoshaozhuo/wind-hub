"""Collector DeviceRuntime 健康检查、连接状态与热更新单元测试。"""

from __future__ import annotations

import asyncio

import pytest

from collector.application.config import CollectorConfig, RuntimeParams
from collector.application.device_runtime import DeviceRuntime, is_connection_level
from collector.application.session import CollectorDeviceSession
from core.domain import Device, DeviceId, PointTable
from tests.support.new_collector import (
    CollectorFakeProtocol,
    make_collector_config,
    make_session,
)


def _runtime(
    proto: CollectorFakeProtocol | None = None,
    *,
    clock=None,
) -> tuple[DeviceRuntime, CollectorFakeProtocol, CollectorConfig]:
    proto = proto or CollectorFakeProtocol()
    config = make_collector_config()
    session = make_session(config, proto)
    runtime = DeviceRuntime(
        {"dev1": session},
        RuntimeParams(connect_timeout=0.2),
        clock=clock or (lambda: 0.0),
    )
    return runtime, proto, config


def _device(config: CollectorConfig, device_id: str = "dev1") -> Device:
    return config.devices[DeviceId(device_id)]


def _table(config: CollectorConfig, device_id: str = "dev1") -> PointTable:
    return config.point_table_for_device(DeviceId(device_id))


def test_is_connection_level_walks_cause_chain():
    assert is_connection_level(TimeoutError())
    assert is_connection_level(ConnectionRefusedError())
    wrapped = RuntimeError("protocol failure")
    wrapped.__cause__ = OSError("socket closed")
    assert is_connection_level(wrapped)
    assert not is_connection_level(ValueError("bad data"))


async def test_ensure_connected_fast_path_when_connected():
    runtime, proto, _config = _runtime()
    await runtime.connect_all()
    assert await runtime.ensure_connected("dev1") is True
    assert proto.connect_calls == 1  # 快路径零开销
    await runtime.close_all()


async def test_normal_acquisition_does_not_retry_connect():
    """采集查询不触发连接，故障恢复由 Watchdog 周期执行。"""
    runtime, proto, _config = _runtime()
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
    runtime, proto, _config = _runtime(clock=lambda: now[0])
    proto.fail_connect = True
    await runtime.connect_all()
    proto.fail_connect = False
    assert await runtime.ensure_connected("dev1", force=True) is True
    await runtime.close_all()


async def test_ensure_connected_unknown_device_false():
    runtime, _, _config = _runtime()
    assert await runtime.ensure_connected("ghost") is False


async def test_report_read_failure_marks_disconnected_on_connection_level():
    runtime, _, _config = _runtime()
    await runtime.connect_all()
    runtime.report_read_failure("dev1", OSError("socket closed"))
    state = runtime.device_state("dev1")
    assert state is not None and not state.connected
    assert state.last_error == "socket closed"
    await runtime.close_all()


async def test_report_read_failure_uses_driver_health_when_not_connection_level():
    runtime, proto, _config = _runtime()
    await runtime.connect_all()
    proto.connected = False  # 驱动自报不健康（库包装了断线但不含 OSError）
    runtime.report_read_failure("dev1", ValueError("weird library error"))
    state = runtime.device_state("dev1")
    assert state is not None and not state.connected
    await runtime.close_all()


async def test_report_read_success_recovers_connected():
    runtime, _, _config = _runtime()
    runtime.report_read_failure("dev1", OSError("down"))
    runtime.report_read_success("dev1")
    state = runtime.device_state("dev1")
    assert state is not None and state.connected and state.last_error is None
    await runtime.close_all()


async def test_add_and_remove_device():
    runtime, _, _config = _runtime()
    config = make_collector_config(device_id="dev2")
    proto2 = CollectorFakeProtocol()
    session2 = make_session(config, proto2, device_id="dev2")
    await runtime.add_device("dev2", _device(config, "dev2"), _table(config, "dev2"), session2)
    assert "dev2" in runtime.devices and proto2.connect_calls == 1

    await runtime.remove_device("dev2")
    assert "dev2" not in runtime.devices
    assert runtime.device_state("dev2") is None
    assert proto2.close_calls == 1


async def test_rebuild_device_closes_old_and_resets_state():
    runtime, proto, _config = _runtime()
    await runtime.connect_all()
    config = make_collector_config()
    proto2 = CollectorFakeProtocol()
    session2 = make_session(config, proto2)
    await runtime.rebuild_device("dev1", _device(config), _table(config), session2)
    assert proto.close_calls == 1
    assert proto2.connect_calls == 1
    state = runtime.device_state("dev1")
    assert state is not None and state.connected and state.consecutive_failures == 0
    await runtime.close_all()


def test_lightweight_update_only_table_or_group_change():
    runtime, _, config = _runtime()
    same_device, same_table = _device(config), _table(config)
    assert runtime.is_lightweight_update("dev1", same_device, same_table)

    # endpoint 变化 → 非轻量
    from dataclasses import replace

    changed_device = replace(same_device, endpoint=replace(same_device.endpoint, port=503))
    assert not runtime.is_lightweight_update("dev1", changed_device, same_table)
    assert runtime.requires_rebuild("dev1", changed_device, same_table)
    assert not runtime.requires_rebuild("dev1", same_device, same_table)


async def test_close_all_bounds_hung_device_close():
    """单台关闭挂起以 connect_timeout 为上界，不阻断其余设备释放。"""

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
    await runtime.rebuild_device("dev1", _device(config), _table(config), new_session)
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
        runtime.rebuild_device(
            "dev1", _device(config), _table(config), new_session
        )
    )
    await asyncio.sleep(0)
    release_connect.set()
    await asyncio.gather(first, rebuild)
    assert await second is False
    assert runtime.devices["dev1"] is new_session


async def _runtime_with_hung_connect(
    connect_timeout: float = 0.2,
) -> tuple[DeviceRuntime, CollectorDeviceSession, _RecordingMetrics, asyncio.Event]:
    """构造 connect 永久挂起的设备运行时（黑洞/无响应对端的确定性替身）。"""

    metrics = _RecordingMetrics()
    config = make_collector_config()
    session = make_session(config, CollectorFakeProtocol())
    runtime = DeviceRuntime(
        {"dev1": session},
        RuntimeParams(connect_timeout=connect_timeout),
        clock=lambda: 0.0,
        metrics_hook=metrics,  # type: ignore[arg-type]
    )
    entered = asyncio.Event()

    async def hung_connect() -> None:
        entered.set()
        await asyncio.Event().wait()

    session.connect = hung_connect  # type: ignore[method-assign]
    return runtime, session, metrics, entered


async def test_watchdog_connect_timeout_is_counted_as_failure():
    """内部 asyncio.timeout 取消挂起的 connect 必须记账（FINDING-RELIAB-BH-01 单元复现）。

    旧实现的记账包装是 except Exception，捕获不到 CancelledError
    （BaseException 子类），黑洞下每次 connect 超时都漏记
    device_connect_failures。
    """

    runtime, _session, metrics, entered = await _runtime_with_hung_connect()
    watchdog = runtime._watchdogs["dev1"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(watchdog.reconnect(), timeout=2.0)
    await entered.wait()
    assert metrics.connect_failed == [("dev1", "modbus")]
    state = runtime.device_state("dev1")
    assert state is not None and state.consecutive_failures == 1


async def test_watchdog_stop_during_hung_connect_is_external_cancel_not_failure():
    """停机取消挂起的 connect：原样传播、不记为一次 connect 失败。"""
    import contextlib

    runtime, _session, metrics, entered = await _runtime_with_hung_connect(connect_timeout=5.0)
    watchdog = runtime._watchdogs["dev1"]
    reconnect = asyncio.create_task(watchdog.reconnect())
    await entered.wait()
    await watchdog.stop()
    # Python 3.11 的 asyncio.timeout 会把停机取消在 reconnect() 边界
    # 转换成 TimeoutError（Watchdog 既有语义，与内部超时同形）；本测试
    # 关心的是停机取消不得记为一次 connect 失败。
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        await reconnect
    assert metrics.connect_failed == []
    state = runtime.device_state("dev1")
    assert state is not None and state.consecutive_failures == 0


async def test_failure_success_failure_connect_metrics_accumulate():
    """失败→恢复→再次失败：失败计数与连续失败状态随周期单调累计/清零。"""

    runtime, session, metrics, _entered = await _runtime_with_hung_connect()
    watchdog = runtime._watchdogs["dev1"]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(watchdog.reconnect(), timeout=2.0)
    assert metrics.connect_failed == [("dev1", "modbus")]

    async def good_connect() -> None:
        return None

    session.connect = good_connect  # type: ignore[method-assign]
    await watchdog.reconnect()
    assert metrics.reconnected == [("dev1", "modbus")]
    state = runtime.device_state("dev1")
    assert state is not None and state.connected and state.consecutive_failures == 0

    gate = asyncio.Event()
    entered2 = asyncio.Event()

    async def hung_again() -> None:
        entered2.set()
        await gate.wait()

    session.connect = hung_again  # type: ignore[method-assign]
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(watchdog.reconnect(), timeout=2.0)
    assert metrics.connect_failed == [("dev1", "modbus"), ("dev1", "modbus")]
    assert state.consecutive_failures == 1
    gate.set()
