"""UC-08 system tests: CLI --check, real process lifecycle and restart recovery."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import pytest

from contracts import collector_pb2 as collector_pb
from contracts import common_pb2 as common
from tests.collector.system.conftest import (
    REPO_ROOT,
    ClientsFactory,
    CollectorHandle,
)
from tests.collector.system.helpers import MODBUS, start_task, wait_actual

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker]


def _run_check(handle: CollectorHandle) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "collector.main",
            "--check",
            "--config",
            str(handle.services / "collector.yaml"),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


async def test_uc08_s01_cli_check_validates_complete_tree(collector: Callable[..., Any]) -> None:
    handle = await collector()
    result = _run_check(handle)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "validated" in result.stdout + result.stderr


async def test_uc08_s02_sigterm_shuts_down_gracefully_and_releases_lock(
    collector, grpc_clients
) -> None:
    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"

    proc = handle.process.process
    proc.terminate()
    assert proc.wait(timeout=20) == 0

    # The state lock is released: a second process on the same state dir starts.
    restarted = await collector(state_dir_override=str(handle.state_dir))
    _channel2, config2, device2 = grpc_clients(restarted.process.grpc_port)
    assert (await config2.GetServiceStatus(common.Empty())).health in {
        "HEALTHY",
        "DEGRADED",
    }
    assert len((await device2.ListTasks(collector_pb.ListTasksRequest())).tasks) == 2


async def test_uc08_s03_kill_restart_resumes_durable_start_intent(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    handle = await collector()
    _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
    await start_task(device_stub, config_stub, MODBUS)
    assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"

    proc = handle.process.process
    proc.kill()  # SIGKILL: no graceful shutdown, state survives on disk
    assert proc.wait(timeout=10) != 0

    restarted = await collector(state_dir_override=str(handle.state_dir))
    _channel2, _config2, device2 = grpc_clients(restarted.process.grpc_port)
    # No StartTask call: persisted desired_state=START converges by itself.
    assert await wait_actual(device2, MODBUS, {"RUNNING"}, timeout=30) == "RUNNING"
    task = await device2.GetTask(common.ObjectId(id=MODBUS))
    assert task.task.desired_state == "START"


async def test_uc08_s03_partial_device_failure_keeps_healthy_device_running(
    collector, grpc_clients
) -> None:
    from tests.collector.system.blackhole import TcpBlackhole

    hole = TcpBlackhole()
    await hole.start()
    try:
        handle = await collector(blackhole_port=hole.port, hold_desired_state="START")
        _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
        await start_task(device_stub, config_stub, MODBUS)
        assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"

        deadline = asyncio.get_running_loop().time() + 15
        status = await config_stub.GetServiceStatus(common.Empty())
        while asyncio.get_running_loop().time() < deadline and status.health != "DEGRADED":
            await asyncio.sleep(0.5)
            status = await config_stub.GetServiceStatus(common.Empty())
        assert status.health == "DEGRADED"
        # The healthy device is not dragged down by the unreachable one.
        assert await wait_actual(device_stub, MODBUS, {"RUNNING"}) == "RUNNING"
        assert len((await device_stub.ListTasks(collector_pb.ListTasksRequest())).tasks) == 3
    finally:
        await hole.stop()


async def test_uc08_s03_unsettled_io_shutdown_exits_nonzero(
    collector: Callable[..., Any], grpc_clients: ClientsFactory
) -> None:
    from tests.collector.system.blackhole import TcpBlackhole

    hole = TcpBlackhole()
    await hole.start()
    try:
        # Shutdown deadline (0.3 s) is shorter than the protocol read timeout
        # (2 s), so a blackholed read is still in flight when SIGTERM arrives.
        handle = await collector(blackhole_port=hole.port, shutdown_timeout=0.3)
        _channel, config_stub, device_stub = grpc_clients(handle.process.grpc_port)
        await start_task(device_stub, config_stub, "modbus-hold")
        await asyncio.sleep(0.5)  # session accepted; no protocol reply ever comes
        read = await device_stub.ReadPoints(
            collector_pb.ReadPointsRequest(
                device_id="modbus-hold", point_ids=["active_power"], timeout_ms=300
            ),
            metadata=(("x-windhub-admin-token", "acceptance-admin-token"),),
        )
        assert read.samples[0].error_code == "TIMEOUT_UNCONFIRMED"
        exit_code = handle.process.stop(timeout=20)
        assert exit_code == 1
        assert "unsettled I/O" in handle.process.log_text()
    finally:
        await hole.stop()
