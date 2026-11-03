"""UC-02 system tests: durable START/STOP intent and STOP precedence."""

from __future__ import annotations

import asyncio

import pytest
import yaml

from contracts import common_pb2 as common
from tests.collector.system.helpers import (
    MODBUS,
    disk_revision,
    publishing,
    push_apply,
    snapshot_files,
    start_task,
    stop_task,
    wait_actual,
)
from tests.collector.system.nats_verify import collect_realtime

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


async def test_uc02_s01_start_samples_then_stop_is_silent(
    collector, grpc_clients, real_nats
) -> None:
    handle = await collector(task_interval=0.2)
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
    messages = await collect_realtime(real_nats.url, count=3, timeout=10)
    assert len(messages) >= 3

    await stop_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"STOPPED"}) == "STOPPED"
    published_after_stop = (await publishing(device_stub)).published
    await asyncio.sleep(1.5)
    assert (await publishing(device_stub)).published == published_after_stop


async def test_uc02_s02_start_while_link_down_converges_after_link_returns(
    collector, grpc_clients, real_nats, modbus_slave
) -> None:
    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await modbus_slave.stop()
    # START while unreachable is accepted and persisted; convergence is pending.
    await start_task(device_stub, config_stub, MODBUS)
    await asyncio.sleep(2.0)
    task = await device_stub.GetTask(common.ObjectId(id=MODBUS))
    assert task.task.desired_state == "START"
    assert task.task.actual_state in {"FAULTED", "ISOLATED", "RECOVERING", "CONNECTING"}
    assert (await config_stub.GetServiceStatus(common.Empty())).health == "DEGRADED"

    await modbus_slave.start()
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}, timeout=30) == "RUNNING"


async def test_uc02_s03_manual_stop_never_revived_by_watchdog_or_reload(
    collector, grpc_clients, real_nats, modbus_slave
) -> None:
    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
    await modbus_slave.stop()
    await asyncio.sleep(1.0)
    await stop_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"STOPPED"}) == "STOPPED"

    # A snapshot re-load while the field link returns: STOP stays authoritative.
    revision = await disk_revision(config_stub)
    files = await snapshot_files(config_stub)
    site = yaml.safe_load(files["instances/site.yaml"])
    site["name"] = "Acceptance Site Renamed"
    files["instances/site.yaml"] = yaml.safe_dump(site, sort_keys=False).encode()
    result = await push_apply(config_stub, files, revision)
    statuses = {item.status for item in result.apply_results}
    assert statuses <= {"APPLIED"}
    await modbus_slave.start()
    await asyncio.sleep(3.0)
    task = await device_stub.GetTask(common.ObjectId(id=MODBUS))
    assert task.task.desired_state == "STOP"
    assert task.task.actual_state == "STOPPED"
