"""UC-03 system tests: real acquisition modes and NATS/JetStream publishing."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import Callable
from typing import Any

import pytest

from contracts import collector_pb2 as collector_pb
from contracts import common_pb2 as common
from tests.collector.system.conftest import ADMIN_METADATA, ClientsFactory, RealNats
from tests.collector.system.delayed_proxy import DelayedTcpProxy
from tests.collector.system.helpers import (
    IEC104,
    MODBUS,
    publishing,
    start_task,
    wait_actual,
)
from tests.collector.system.nats_verify import (
    collect_realtime,
    collect_reliable,
    ensure_hist_stream,
    point_docs,
)
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


async def test_uc03_s01_poll_publishes_complete_envelope(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
) -> None:
    handle = await collector(task_interval=0.2)
    _channel, _config, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, _config, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
    batches = await collect_realtime(real_nats.url, count=6, timeout=10)
    # v2：一个采集周期形成一个设备级批次，信封与批次身份在批次层，逐点值在 samples 内。
    for batch in batches:
        assert batch["schema_version"] == 2
        assert batch["wind_farm_id"] == "site_alpha"
        assert batch["device_id"] == MODBUS
        assert batch["mode"] == "realtime"
        assert batch["batch_id"]
        assert batch["samples"]
    messages = point_docs(batches)
    by_point: dict[str, dict[str, Any]] = {}
    for message in messages:
        assert message["quality"] == "good"
        assert message["sample_id"]
        by_point[message["point_id"]] = message
    assert by_point["active_power"]["value"] == 1500
    assert abs(float(by_point["rotor_speed"]["value"]) - 12.5) < 1e-6
    assert by_point["gear_temp"]["value"] == 42
    status = await publishing(device_stub)
    assert status.channel == "realtime"
    assert status.published >= 6
    assert status.failed == 0


async def test_uc03_s01_listen_receives_spontaneous_iec104(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
    iec104_slave: IEC104MockServer,
) -> None:
    handle = await collector(task_interval=0.2)
    _channel, _config, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, _config, IEC104)
    await wait_actual(device_stub, IEC104, {"STARTING", "RUNNING"})
    await iec104_slave.set_value(100, 1610.25, spontaneous=True)
    batches = await collect_realtime(real_nats.url, count=1, timeout=12)
    messages = point_docs(batches)
    power = next(item for item in messages if item["point_id"] == "meas_power")
    assert abs(float(power["value"]) - 1610.25) < 1e-6
    assert power["device_id"] == IEC104


@pytest.mark.ads
@pytest.mark.hardware
async def test_uc03_s01_subscribe_requires_ads(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
) -> None:
    """SUBSCRIBE is the ADS native-push mode; it needs a real TwinCAT PLC."""
    if not os.getenv("WIND_HUB_TEST_ADS_ROUTER"):
        pytest.skip("NOT_RUN: no ADS/TwinCAT environment for native SUBSCRIBE")


async def test_uc03_s01_reliable_delivery_is_pubacked_in_jetstream(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
) -> None:
    await ensure_hist_stream(real_nats.url)
    handle = await collector(delivery="reliable", task_interval=0.2)
    _channel, _config, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, _config, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
    messages = await collect_reliable(real_nats.url, count=3, timeout=15)
    assert len(messages) == 3
    assert {item["device_id"] for item in messages} == {MODBUS}
    status = await publishing(device_stub)
    assert status.channel == "reliable"
    assert status.published >= 3


async def test_uc03_s02_interrogation_reports_missing_points(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
) -> None:
    partial_slave = IEC104MockServer(stations={1: {100: 1500.5}})
    await partial_slave.start()
    try:
        handle = await collector(iec104_port_override=partial_slave.port)
        _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
        await start_task(device_stub, config_stub, IEC104)
        await asyncio.sleep(1.0)
        result = await device_stub.InterrogateDevice(
            collector_pb.InterrogateDeviceRequest(device_id=IEC104, timeout_ms=5000),
            metadata=(("x-windhub-admin-token", "acceptance-admin-token"),),
        )
        assert result.status == "PARTIAL"
        assert result.missing_point_ids == ["meas_freq"]
        assert {sample.point_id for sample in result.samples} == {"meas_power"}
    finally:
        await partial_slave.stop()


async def test_uc03_s02_overdue_poll_increments_skipped_slots(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
    modbus_slave: ModbusMockServer,
) -> None:
    # A real TCP proxy holding every Modbus response for 0.5 s while the task
    # cadence is 0.2 s makes polls genuinely miss slots on the wire; the
    # Collector must count the skipped slots rather than silently drift.
    proxy = DelayedTcpProxy("127.0.0.1", modbus_slave.port, response_delay=0.5)
    await proxy.start()
    try:
        handle = await collector(modbus_port_override=proxy.port, task_interval=0.2)
        _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
        await start_task(device_stub, config_stub, MODBUS)
        assert await wait_actual(device_stub, MODBUS, {"RUNNING"}, timeout=30) == "RUNNING"
        await asyncio.sleep(4.0)

        deadline = asyncio.get_running_loop().time() + 10
        skipped = 0.0
        while asyncio.get_running_loop().time() < deadline:
            response = await config_stub.GetMetrics(common.GetMetricsRequest())
            values = [m.value for m in response.metrics if m.name == "collector.poll.skipped_slots"]
            skipped = values[0] if values else 0.0
            if skipped >= 1:
                break
            await asyncio.sleep(0.5)
        assert skipped >= 1
    finally:
        await proxy.stop()


async def test_uc03_s02_iec104_handshake_against_blackhole_is_bounded(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
) -> None:
    # An IEC104 peer that accepts TCP and never completes STARTDT must not wedge
    # the device: bounded establishment deadlines surface FAULTED/ISOLATED, the
    # watchdog keeps making recovery decisions, and failure events are recorded.
    # (The core IEC104 listen() call itself is non-blocking; the matching bound
    # around listen()/subscription.start() additionally covers ADS, which has no
    # real TwinCAT environment here and stays NOT_RUN.)
    from tests.collector.system.blackhole import TcpBlackhole

    hole = TcpBlackhole()
    await hole.start()
    try:
        handle = await collector(iec104_port_override=hole.port, watchdog_interval=0.3)
        _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
        await start_task(device_stub, config_stub, IEC104)
        state = await wait_actual(device_stub, IEC104, {"FAULTED", "ISOLATED"}, timeout=25)
        assert state in {"FAULTED", "ISOLATED"}

        # The failure is real evidence, not a silent hang: events are recorded.
        deadline = asyncio.get_running_loop().time() + 10
        seen = 0
        while asyncio.get_running_loop().time() < deadline:
            events = await config_stub.GetEvents(
                common.GetEventsRequest(page_size=50), metadata=ADMIN_METADATA
            )
            seen = len(events.events)
            if seen:
                break
            await asyncio.sleep(0.5)
        assert seen >= 1
    finally:
        await hole.stop()


async def test_uc03_s03_nats_outage_then_recovery_resumes_publishing(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
) -> None:
    handle = await collector(task_interval=0.2)
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
    await collect_realtime(real_nats.url, count=3, timeout=10)
    failed_before = (await publishing(device_stub)).failed

    async def _counter(name: str) -> float:
        response = await config_stub.GetMetrics(common.GetMetricsRequest())
        for metric in response.metrics:
            if metric.name == name:
                return float(metric.value)
        return 0.0

    connection_failed_before = await _counter("collector.publisher.connection_failed")
    dropped_before = await _counter("collector.publisher.dropped")

    stopped = subprocess.run(
        ["docker", "stop", real_nats.container], capture_output=True, text=True, timeout=30
    )
    assert stopped.returncode == 0
    deadline = asyncio.get_running_loop().time() + 20
    status = await publishing(device_stub)
    while asyncio.get_running_loop().time() < deadline:
        status = await publishing(device_stub)
        connection_failed = await _counter("collector.publisher.connection_failed")
        if not status.nats_connected and connection_failed > connection_failed_before:
            break
        await asyncio.sleep(0.5)
    assert not status.nats_connected
    # D-15: an unreachable broker is one dropped outcome with an independent
    # connection_failed attribute; it is neither a definite failure nor an
    # unconfirmed send.
    assert (await _counter("collector.publisher.connection_failed")) > connection_failed_before
    assert (await _counter("collector.publisher.dropped")) > dropped_before
    assert status.failed == failed_before
    started = subprocess.run(
        ["docker", "start", real_nats.container], capture_output=True, text=True, timeout=30
    )
    assert started.returncode == 0

    deadline = asyncio.get_running_loop().time() + 40
    while asyncio.get_running_loop().time() < deadline:
        status = await publishing(device_stub)
        if status.nats_connected:
            break
        await asyncio.sleep(1.0)
    assert status.nats_connected
    assert await collect_realtime(real_nats.url, count=3, timeout=15)
