"""设备子系统 ownership、失败记账与重复重建释放的直接验证。

使用真实 CollectorDeviceSession/DeviceRuntime 与 mock ProtocolPort；协议 client
的实际释放及网络恢复另由 Modbus lifecycle、recovery 和 netem 测试覆盖。
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from wind_hub_collector.application.runtime import CollectorDeviceSession, CollectorRuntime
from wind_hub_collector.application.runtime.device_runtime import DeviceRuntime
from wind_hub_collector.application.runtime.metrics_state import CollectorMetricsState
from wind_hub_collector.domain.acquisition import AcquisitionEngine
from wind_hub_core.config import DeviceConfig, RuntimeConfig
from wind_hub_core.model.device import Endpoint
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.protocol.port import ProtocolPort


def _config(device_id: str = "d1", port: int = 502) -> DeviceConfig:
    return DeviceConfig(
        device_id=device_id, protocol="modbus", point_table="t1",
        endpoint=Endpoint(host="127.0.0.1", port=port),
    )


def _protocol() -> MagicMock:
    protocol = MagicMock(spec=ProtocolPort)
    protocol.connect = AsyncMock()
    protocol.close = AsyncMock()
    protocol.health.return_value = HealthStatus(healthy=True)
    return protocol


def test_collector_transfers_registry_and_engine_device_port_to_one_owner() -> None:
    session = CollectorDeviceSession(_config(), [], _protocol())
    engine = AcquisitionEngine()
    collector = CollectorRuntime(
        devices={"d1": session}, sinks={}, engine=engine, config=RuntimeConfig(),
    )

    devices = collector.device_runtime
    assert isinstance(devices, DeviceRuntime)
    assert collector.devices is devices.devices
    assert devices.devices == {"d1": session}
    assert devices.device_state("d1") is not None
    assert devices.device_state("d1").connected is False
    assert devices.device_state("missing") is None
    assert not {"_devices", "_device_states", "_protocol_factory"} & vars(collector).keys()
    # 引擎直接绑定设备 owner，Collector 不再实现另一份连接状态端口。
    assert engine._device_state is devices


async def test_add_retry_and_remove_keep_session_state_pair_even_on_close_failure() -> None:
    protocol = _protocol()
    protocol.connect.side_effect = OSError("offline")
    devices = DeviceRuntime({}, RuntimeConfig(), clock=lambda: 10.0)
    cfg = _config()

    await devices.add_device("d1", cfg, protocol, [])
    session = devices.devices["d1"]
    state = devices.device_state("d1")
    assert state is not None
    assert state.connected is False
    assert state.consecutive_failures == 1
    assert state.next_retry_at == 11.0

    protocol.connect.side_effect = None
    unused = _protocol()
    await devices.add_device("d1", cfg, unused, [])
    assert devices.devices["d1"] is session
    assert devices.device_state("d1") is state
    assert state.connected is True
    assert state.consecutive_failures == 0
    unused.connect.assert_not_awaited()

    protocol.close.side_effect = OSError("close failed")
    await devices.remove_device("d1")
    await devices.remove_device("d1")
    protocol.close.assert_awaited_once()
    assert devices.devices == {}
    assert devices.device_state("d1") is None
    assert await devices.ensure_connected("d1") is False
    assert devices.device_state("d1") is None


async def test_repeated_rebuild_closes_each_old_session_once_and_resets_state() -> None:
    protocols = [_protocol() for _ in range(11)]
    devices = DeviceRuntime({}, RuntimeConfig(), clock=lambda: 10.0)
    await devices.add_device("d1", _config(), protocols[0], [])
    state = devices.device_state("d1")
    try:
        for index, protocol in enumerate(protocols[1:], start=1):
            if index % 2:
                protocol.connect.side_effect = OSError("offline")
            await devices.rebuild_device("d1", _config(port=502 + index), protocol, [])
            protocols[index - 1].close.assert_awaited_once()
            assert set(devices.devices) == {"d1"}
            assert devices.devices["d1"].protocol is protocol
            updated = devices.device_state("d1")
            assert updated is not None and updated is not state
            assert updated.connected is (index % 2 == 0)
            assert updated.consecutive_failures == index % 2
            state = updated
    finally:
        await devices.remove_device("d1")
    assert devices.devices == {}
    assert devices.device_state("d1") is None
    for protocol in protocols:
        protocol.connect.assert_awaited_once()
        protocol.close.assert_awaited_once()


async def test_startup_timeout_and_shutdown_failure_do_not_block_other_devices() -> None:
    failed, healthy = _protocol(), _protocol()
    cancelled = asyncio.Event()

    async def blocked_connect() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    failed.connect.side_effect = blocked_connect
    failed.close.side_effect = OSError("close failed")
    metrics = CollectorMetricsState()
    devices = DeviceRuntime(
        {"d1": CollectorDeviceSession(_config(), [], failed),
         "d2": CollectorDeviceSession(_config("d2"), [], healthy)},
        RuntimeConfig(connect_timeout=0.01), metrics_hook=metrics,
    )
    await devices.connect_all()
    try:
        assert cancelled.is_set()
        assert devices.device_state("d1").last_error == "connect timeout"
        assert devices.device_state("d1").consecutive_failures == 1
        assert devices.device_state("d2").connected is True
        assert metrics.snapshot()["device_connect_failures"] == {"d1": 1}
        assert metrics.snapshot()["device_reconnects"] == {}
    finally:
        await devices.close_all()
    failed.close.assert_awaited_once()
    healthy.close.assert_awaited_once()


async def test_reconnect_backoff_and_metrics_hook_replacement() -> None:
    now = 10.0
    protocol = _protocol()
    protocol.connect.side_effect = OSError("offline")
    first, replacement = CollectorMetricsState(), CollectorMetricsState()
    collector = CollectorRuntime(
        devices={"d1": CollectorDeviceSession(_config(), [], protocol)},
        sinks={}, engine=AcquisitionEngine(), config=RuntimeConfig(),
        clock=lambda: now, metrics_hook=first,
    )
    devices = collector.device_runtime
    await collector.start()
    try:
        state = devices.device_state("d1")
        assert state.next_retry_at == 11.0
        assert await devices.ensure_connected("d1") is False
        protocol.connect.assert_awaited_once()
        collector.attach_metrics_hook(replacement)
        now = 11.0
        assert await devices.ensure_connected("d1") is False
        assert state.next_retry_at == 13.0
        assert state.consecutive_failures == 2
        now = 12.9
        assert await devices.ensure_connected("d1") is False
        assert protocol.connect.await_count == 2
        now = 13.0
        protocol.connect.side_effect = None
        assert await devices.ensure_connected("d1") is True
        assert state.connected is True
        assert state.consecutive_failures == 0
        assert state.last_success_at == 13.0
        assert first.snapshot()["device_connect_failures"] == {"d1": 1}
        assert first.snapshot()["device_reconnects"] == {}
        assert replacement.snapshot()["device_connect_failures"] == {"d1": 1}
        assert replacement.snapshot()["device_reconnects"] == {"d1": 1}
        collector.attach_metrics_hook(None)
        devices.report_read_failure("d1", OSError("connection lost"))
        assert state.connected is False
        now = 14.0
        assert await devices.ensure_connected("d1") is True
        assert replacement.snapshot()["device_reconnects"] == {"d1": 1}
    finally:
        await collector.stop()


@pytest.mark.parametrize("wrapped", [False, True])
async def test_read_failure_classification_keeps_connection_only_for_healthy_driver(
    wrapped: bool,
) -> None:
    protocol = _protocol()
    devices = DeviceRuntime(
        {"d1": CollectorDeviceSession(_config(), [], protocol)}, RuntimeConfig(),
    )
    await devices.connect_all()
    try:
        error = RuntimeError("read failed")
        if wrapped:
            error.__cause__ = OSError("connection lost")
        devices.report_read_failure("d1", error)
        assert devices.device_state("d1").connected is (not wrapped)
        devices.report_read_success("d1")
        protocol.health.return_value = HealthStatus(healthy=False)
        devices.report_read_failure("d1", RuntimeError("driver reports disconnected"))
        assert devices.device_state("d1").connected is False
    finally:
        await devices.close_all()
