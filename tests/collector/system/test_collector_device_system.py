"""UC-01 system tests: real device sessions, health isolation and recovery."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

import grpc
import pytest

from contracts import collector_pb2 as collector_pb
from contracts import common_pb2 as common
from contracts.common_pb2_grpc import CommonServiceStub
from tests.collector.system.conftest import ADMIN_METADATA, ClientsFactory, RealNats
from tests.collector.system.helpers import (
    IEC104,
    MODBUS,
    disk_revision,
    start_task,
    wait_actual,
)
from tests.fixtures.servers.iec104_server import IEC104MockServer
from tests.fixtures.servers.modbus_server import ModbusMockServer

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


# ---------------------------------------------------------------------------
# UC-01-S01 normal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("device_id", [MODBUS, IEC104])
async def test_uc01_s01_real_device_connect_and_health(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
    device_id: str,
) -> None:
    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, device_id)
    # Modbus POLL reaches RUNNING on the first batch; IEC104 LISTEN is STARTING
    # until a spontaneous or interrogated sample proves the data path.
    if device_id == MODBUS:
        assert await wait_actual(device_stub, device_id, {"RUNNING"}) == "RUNNING"
    else:
        assert await wait_actual(device_stub, device_id, {"RUNNING", "STARTING"}) in {
            "RUNNING",
            "STARTING",
        }
    status = await config_stub.GetServiceStatus(common.Empty())
    assert status.health in {"HEALTHY", "DEGRADED"}
    diagnose = await config_stub.Diagnose(
        common.DiagnoseRequest(target_id=device_id, stages=["NETWORK", "PROTOCOL"]),
        metadata=(("x-windhub-admin-token", "acceptance-admin-token"),),
    )
    assert {stage.stage: stage.status for stage in diagnose.stages} == {
        "NETWORK": "PASS",
        "PROTOCOL": "PASS",
    }


@pytest.mark.ads
@pytest.mark.hardware
async def test_uc01_s01_ads_real_device(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
) -> None:
    """ADS requires a real TwinCAT router; never impersonated by a simulated link."""
    if not os.getenv("WIND_HUB_TEST_ADS_ROUTER"):
        pytest.skip("NOT_RUN: no TwinCAT ADS router provided (set WIND_HUB_TEST_ADS_ROUTER)")


# ---------------------------------------------------------------------------
# UC-01-S02 boundary
# ---------------------------------------------------------------------------


async def _connection_count(config_stub: CommonServiceStub) -> float:
    metrics = await config_stub.GetMetrics(common.GetMetricsRequest(scope="device:modbus-1"))
    counts = [m.value for m in metrics.metrics if m.name.endswith(".connections")]
    return counts[0] if counts else 0.0


async def test_uc01_s02_duplicate_start_is_idempotent(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
) -> None:
    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
    connects_before = await _connection_count(config_stub)

    # A repeated START at the new revision must not tear down the session.
    await start_task(device_stub, config_stub, MODBUS)
    await asyncio.sleep(1.0)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
    assert await _connection_count(config_stub) == connects_before


async def test_uc01_s02_wrong_endpoint_is_reported_per_device(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
    modbus_slave: ModbusMockServer,
) -> None:
    from tests.collector.system.conftest import free_port

    del modbus_slave
    handle = await collector(modbus_port_override=free_port())
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, MODBUS)
    deadline = asyncio.get_running_loop().time() + 20
    detail = ""
    while asyncio.get_running_loop().time() < deadline:
        status = await config_stub.GetServiceStatus(common.Empty())
        if status.health == "DEGRADED":
            detail = next(
                (dep.detail for dep in status.dependencies if dep.name == "acquisition"), ""
            )
            if MODBUS in detail:
                break
        await asyncio.sleep(0.5)
    assert status.health == "DEGRADED"
    assert MODBUS in detail
    assert await wait_actual(
        device_stub, MODBUS, {"FAULTED", "ISOLATED", "RECOVERING", "CONNECTING"}
    ) in {"FAULTED", "ISOLATED", "RECOVERING", "CONNECTING"}


async def test_uc01_s02_disabled_device_rejects_start(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
) -> None:
    handle = await collector(disabled_devices=(MODBUS,))
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    # Disabled devices are excluded from the active inventory; START is NOT_FOUND.
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await device_stub.StartTask(
            common.OperationRequest(
                id=MODBUS,
                expected_revision=await disk_revision(config_stub),
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND

    with pytest.raises(grpc.aio.AioRpcError) as unknown:
        await device_stub.StartTask(
            common.OperationRequest(
                id="modbus-does-not-exist",
                expected_revision=await disk_revision(config_stub),
            ),
            metadata=ADMIN_METADATA,
        )
    assert unknown.value.code() == grpc.StatusCode.NOT_FOUND

    # Read-only inventory agrees: the disabled device is simply absent.
    listing = await device_stub.ListDevices(collector_pb.ListDevicesRequest())
    assert MODBUS not in {item.device_id for item in listing.devices}


# ---------------------------------------------------------------------------
# UC-01-S03 recovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("protocol", ["modbus", "iec104"])
async def test_uc01_s03_link_loss_watchdog_reconnects(
    collector: Callable[..., Any],
    grpc_clients: ClientsFactory,
    real_nats: RealNats,
    modbus_slave: ModbusMockServer,
    iec104_slave: IEC104MockServer,
    protocol: str,
) -> None:
    device_id = MODBUS if protocol == "modbus" else IEC104
    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, device_id)
    if protocol == "modbus":
        assert await wait_actual(device_stub, device_id, {"RUNNING"}) == "RUNNING"

    slave = modbus_slave if protocol == "modbus" else iec104_slave
    await slave.stop()
    await asyncio.sleep(2.0)
    assert (await config_stub.GetServiceStatus(common.Empty())).health == "DEGRADED"

    await slave.start()
    expected = {"RUNNING"} if protocol == "modbus" else {"STARTING", "RUNNING"}
    assert await wait_actual(device_stub, device_id, expected, timeout=30) in expected
    task = await device_stub.GetTask(common.ObjectId(id=device_id))
    assert task.task.desired_state == "START"
