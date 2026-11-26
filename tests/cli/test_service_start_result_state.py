"""Service lifecycle command exit codes must not conflate process and health."""

from __future__ import annotations

import pytest

from cli.runtime import Runtime


@pytest.mark.parametrize("health", ["DEGRADED", "UNKNOWN", "UNAVAILABLE", "FAILED"])
def test_confirmed_service_start_reports_process_success(health: str) -> None:
    """The caller still sees degraded health in the returned payload."""
    result = {
        "process": {
            "manager": "process",
            "confirmed": True,
            "running": True,
            "state": "RUNNING",
            "stages": [{"operation": "start", "confirmed": True}],
        },
        "business": {"health": health},
    }
    assert Runtime._result_state("service start", result) == ("SUCCESS", 0)
    assert result["business"]["health"] == health


@pytest.mark.parametrize("command", ["service start", "service restart"])
def test_unconfirmed_process_is_never_success(command: str) -> None:
    """Unknown process state and failed stages must not be accepted."""
    missing = {"process": {"confirmed": True}, "business": {"health": "READY"}}
    assert Runtime._result_state(command, missing) == ("UNKNOWN", 3)

    failed_stage = {
        "process": {
            "confirmed": True,
            "running": True,
            "stages": [{"operation": "start", "confirmed": False}],
        },
        "business": {"health": "HEALTHY"},
    }
    assert Runtime._result_state(command, failed_stage) == ("UNKNOWN", 3)


def test_service_status_still_reports_unhealthy_business() -> None:
    """Reporting a successful start cannot mask a later health failure."""
    status = [
        {
            "process": {"manager": "process", "running": True, "state": "RUNNING"},
            "business": {"health": "FAILED"},
        }
    ]
    assert Runtime._result_state("service status", status) == ("UNKNOWN", 3)


def test_confirmed_stop_is_independent_of_business_health() -> None:
    result = {
        "process": {"confirmed": True, "running": False, "state": "STOPPED"},
        "business": {"health": "UNAVAILABLE"},
    }
    assert Runtime._result_state("service stop", result) == ("SUCCESS", 0)
