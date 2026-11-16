"""E2E-09: formal CLI lifecycle of every full-chain business process."""

from __future__ import annotations

from typing import Any

import pytest

from tests.cli.test_r1f3_strict_cli_e2e import SERVICES

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


async def test_e2e_09_cli_lifecycle_and_worker_cleanup(full_chain: Any) -> None:
    """Restart each CLI-managed process and distinguish PID from business READY."""
    harness = full_chain
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

        after = _status_payload(
            await harness.cli_call(harness.cli_rt, "service", "status", service)
        )
        assert after["process"]["pid"] == process["pid"]
        assert after["business"]["health"] in {"HEALTHY", "DEGRADED"}
        if service == "sink":
            dependencies = after["business"]["dependencies"]
            worker_health = {
                item["name"]: item["health"] for item in dependencies if "worker." in item["name"]
            }
            assert worker_health == {"worker.realtime": "RUNNING", "worker.reliable": "RUNNING"}
        harness.record(
            "E2E-09",
            service=service,
            previous_pid=before_pid,
            pid=process["pid"],
            business_health=after["business"]["health"],
        )
