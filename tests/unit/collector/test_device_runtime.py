"""新 Collector DeviceRuntime 重连节流/故障分类/热更新单元测试。"""

from __future__ import annotations

import pytest

from collector.application.config import RuntimeParams
from collector.application.device_runtime import DeviceRuntime, is_connection_level
from collector.application.device_state import reconnect_delay
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


def test_reconnect_delay_exponential_with_cap():
    assert reconnect_delay(1) == 1.0
    assert reconnect_delay(2) == 2.0
    assert reconnect_delay(3) == 4.0
    assert reconnect_delay(10) == 30.0
    assert reconnect_delay(0) == 1.0


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


async def test_ensure_connected_throttled_within_backoff_window():
    now = [100.0]
    runtime, proto = _runtime(clock=lambda: now[0])
    proto.fail_connect = True
    await runtime.connect_all()  # 失败 → next_retry_at = 100 + 1
    state = runtime.device_state("dev1")
    assert state is not None and not state.connected
    assert state.consecutive_failures == 1

    now[0] = 100.5  # 仍在节流窗口内
    assert await runtime.ensure_connected("dev1") is False
    assert proto.connect_calls == 1  # 不发起新 connect

    now[0] = 101.5  # 窗口已过 → 重试并再次失败 → backoff 2s
    assert await runtime.ensure_connected("dev1") is False
    assert proto.connect_calls == 2
    state = runtime.device_state("dev1")
    assert state is not None and state.next_retry_at == pytest.approx(101.5 + 2.0)

    now[0] = 104.0
    proto.fail_connect = False
    assert await runtime.ensure_connected("dev1") is True
    state = runtime.device_state("dev1")
    assert state is not None and state.connected and state.consecutive_failures == 0


async def test_ensure_connected_force_ignores_backoff():
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

    hung.close = blocked_close
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


async def test_transparent_reconnect_in_read_path_marks_state_and_metric():
    """RecoveringProtocol 读路径内透明重连：状态恢复 connected 且 device_reconnected 记账。"""
    from core.application.recovery import RecoveringProtocol, RecoverySettings

    events: list[tuple[str, str]] = []

    class _Metrics:
        def device_reconnected(self, device_id: str, protocol: str) -> None:
            events.append((device_id, protocol))

    proto = CollectorFakeProtocol()
    port = RecoveringProtocol(proto, RecoverySettings(reconnect_attempts=1))
    config = make_collector_config()
    session = make_session(config, port)  # type: ignore[arg-type]
    runtime = DeviceRuntime(
        {"dev1": session},
        RuntimeParams(connect_timeout=0.2),
        clock=lambda: 0.0,
        metrics_hook=_Metrics(),  # type: ignore[arg-type]
    )
    await runtime.connect_all()

    # 驱动掉线后第一次读触发 RecoveringProtocol 透明重连。
    proto.connected = False
    await port.read_one("p1")

    assert events == [("dev1", "modbus")]
    state = runtime.device_state("dev1")
    assert state is not None and state.connected is True


def _recovery_session(proto, config=None):  # type: ignore[no-untyped-def]
    """构造包裹 RecoveringProtocol 的会话（透明重连测试用）。"""
    from core.application.recovery import RecoveringProtocol, RecoverySettings

    port = RecoveringProtocol(proto, RecoverySettings(reconnect_attempts=1))
    session = make_session(config or make_collector_config(), port)  # type: ignore[arg-type]
    return session, port


class _RecordingMetrics:
    def __init__(self) -> None:
        self.reconnected: list[tuple[str, str]] = []
        self.connect_failed: list[tuple[str, str]] = []

    def device_reconnected(self, device_id: str, protocol: str) -> None:
        self.reconnected.append((device_id, protocol))

    def device_connect_failed(self, device_id: str, protocol: str) -> None:
        self.connect_failed.append((device_id, protocol))


async def test_stale_session_reconnect_hook_does_not_pollute_new_session():
    """设备重建后，旧会话 RecoveringProtocol 的迟到透明重连不得改写新会话状态/指标。"""
    from core.application.recovery import RecoveringProtocol

    metrics = _RecordingMetrics()
    old_proto = CollectorFakeProtocol()
    old_session, old_port = _recovery_session(old_proto)
    config = make_collector_config()
    runtime = DeviceRuntime(
        {"dev1": old_session},
        RuntimeParams(connect_timeout=0.2),
        clock=lambda: 0.0,
        metrics_hook=metrics,  # type: ignore[arg-type]
    )
    await runtime.connect_all()
    assert isinstance(old_session.protocol, RecoveringProtocol)

    # 重建设备：旧会话关闭，新会话接入。
    new_proto = CollectorFakeProtocol()
    new_session, _ = _recovery_session(new_proto)
    await runtime.rebuild_device("dev1", config.device_view("dev1"), new_session)  # type: ignore[arg-type]
    state = runtime.device_state("dev1")
    assert state is not None and state.connected and state.last_success_at == 0.0
    assert metrics.reconnected == []

    # 旧会话的在途读触发透明重连（回调晚于新会话注册）——必须被身份守卫丢弃。
    old_proto.connected = False
    await old_port.read_one("p1")
    assert metrics.reconnected == []
    state = runtime.device_state("dev1")
    assert state is not None and state.last_success_at == 0.0
    assert state.consecutive_failures == 0


async def test_removed_device_reconnect_hook_is_inert():
    """设备移除后，旧会话的迟到重连回调安全无效（无状态、无指标、无异常）。"""
    metrics = _RecordingMetrics()
    old_proto = CollectorFakeProtocol()
    old_session, old_port = _recovery_session(old_proto)
    runtime = DeviceRuntime(
        {"dev1": old_session},
        RuntimeParams(connect_timeout=0.2),
        clock=lambda: 0.0,
        metrics_hook=metrics,  # type: ignore[arg-type]
    )
    await runtime.connect_all()
    await runtime.remove_device("dev1")

    old_proto.connected = False
    await old_port.read_one("p1")  # 旧会话仍可恢复自身连接，但回调不得触及运行时
    assert metrics.reconnected == []
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
