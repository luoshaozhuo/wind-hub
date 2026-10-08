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
