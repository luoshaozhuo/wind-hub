"""E2E-09: formal CLI lifecycle of every full-chain business process."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

import pytest

from tests.cli.test_r1f3_strict_cli_e2e import SERVICES

from .conftest import SIM_DEVICES

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]


def _status_payload(result: dict[str, Any]) -> dict[str, Any]:
    data = result["data"]
    assert isinstance(data, list) and len(data) == 1
    return data[0]


def _identity(pid: int) -> tuple[str, str] | None:
    """Return Linux process state/start tick without trusting a reusable PID."""
    try:
        fields = (Path("/proc") / str(pid) / "stat").read_text(encoding="utf-8").split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return fields[2], fields[21]


def _process_tree(pid: int) -> dict[int, tuple[str, str]]:
    pending = [pid]
    identities: dict[int, tuple[str, str]] = {}
    while pending:
        current = pending.pop()
        identity = _identity(current)
        if identity is None or current in identities:
            continue
        identities[current] = identity
        children = Path("/proc") / str(current) / "task" / str(current) / "children"
        with contextlib.suppress(FileNotFoundError, ProcessLookupError):
            pending.extend(int(item) for item in children.read_text().split())
    return identities


async def _wait_tree_reaped(identities: dict[int, tuple[str, str]], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        remaining = {
            pid: current
            for pid, original in identities.items()
            if (current := _identity(pid)) is not None and current[1] == original[1]
        }
        if not remaining:
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"process identities not reaped: {remaining}")


async def test_e2e_09_cli_lifecycle_and_worker_cleanup(full_chain: Any) -> None:
    """Restart each CLI-managed process and distinguish PID from business READY."""
    harness = full_chain
    for config in (harness.cli_rt, harness.cli_rel):
        for device_id in SIM_DEVICES:
            await harness.cli_call(config, "collector", "start", device_id, "--yes")
    for service in SERVICES:
        before = _status_payload(
            await harness.cli_call(harness.cli_rt, "service", "status", service)
        )
        before_process = before["process"]
        assert before_process["running"] is True
        assert before_process["state"] == "RUNNING"
        before_pid = before_process["pid"]
        assert isinstance(before_pid, int) and before_pid > 1

        # Collector may report a transient NATS DEGRADED business state (CLI
        # exit 3) although native stop/start has completed.  Process identity
        # and subsequent business state are intentionally asserted separately.
        restarted = await harness.cli_call(
            harness.cli_rt, "service", "restart", service, "--yes", check=False
        )
        process = restarted["data"]["process"]
        assert process["running"] is True
        assert process["state"] == "RUNNING"
        assert process["pid"] != before_pid

        after = await harness.poll(
            harness.cli_rt,
            "service",
            "status",
            service,
            predicate=lambda data: bool(data) and data[0]["business"]["health"] == "HEALTHY",
            timeout=60,
        )
        after = after[0]
        assert after["process"]["pid"] == process["pid"]
        assert after["business"]["health"] == "HEALTHY"
        if service == "sink":
            dependencies = after["business"]["dependencies"]
            worker_health = {
                item["name"]: item["health"] for item in dependencies if "worker." in item["name"]
            }
            assert worker_health == {"worker.realtime": "RUNNING", "worker.reliable": "RUNNING"}

        # Repeated start is idempotent and cannot create a second service.
        idempotent = await harness.cli_call(
            harness.cli_rt, "service", "start", service, "--yes", check=False
        )
        same = idempotent["data"]["process"]
        assert same["already_running"] is True
        assert same["pid"] == process["pid"]

        # Capture the true Linux identity of the process and all current workers
        # before stopping.  A vanished PID or a different start tick is reaped;
        # a same-identity zombie is not accepted as cleanup.
        identities = _process_tree(process["pid"])
        assert process["pid"] in identities
        stopped = await harness.cli_call(harness.cli_rt, "service", "stop", service, "--yes")
        stopped_process = stopped["data"]["process"]
        assert stopped_process["confirmed"] is True
        assert stopped_process["running"] is False
        assert stopped_process["state"] == "STOPPED"
        await _wait_tree_reaped(identities)
        stopped_status = await harness.cli_call(
            harness.cli_rt, "service", "status", service, check=False
        )
        assert stopped_status["status"] == "UNKNOWN"
        status = _status_payload(stopped_status)
        assert status["process"]["running"] is False
        assert status["process"]["state"] == "STOPPED"

        stopped_again = await harness.cli_call(harness.cli_rt, "service", "stop", service, "--yes")
        assert stopped_again["data"]["process"]["already_stopped"] is True

        restored = await harness.cli_call(
            harness.cli_rt, "service", "start", service, "--yes", check=False
        )
        restored_process = restored["data"]["process"]
        assert restored_process["confirmed"] is True
        assert restored_process["pid"] != process["pid"]
        ready = await harness.poll(
            harness.cli_rt,
            "service",
            "status",
            service,
            predicate=lambda data: bool(data) and data[0]["business"]["health"] == "HEALTHY",
            timeout=60,
        )
        if service == "sink":
            dependencies = ready[0]["business"]["dependencies"]
            restored_workers = {
                item["name"]: item["health"] for item in dependencies if "worker." in item["name"]
            }
            assert restored_workers == {
                "worker.realtime": "RUNNING",
                "worker.reliable": "RUNNING",
            }
        harness.record(
            "E2E-09",
            service=service,
            previous_pid=before_pid,
            pid=process["pid"],
            restored_pid=restored_process["pid"],
            reaped_pids=sorted(identities),
            business_health=after["business"]["health"],
        )
