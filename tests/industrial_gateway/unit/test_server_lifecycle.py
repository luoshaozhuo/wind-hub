"""ServerLifecycle 纯状态机单元测试。"""

from __future__ import annotations

import pytest

from industrial_gateway.application.server_lifecycle import (
    ActualState,
    DesiredState,
    ServerLifecycle,
)

pytestmark = pytest.mark.unit


def test_start_stop_happy_path() -> None:
    lifecycle = ServerLifecycle(DesiredState.START)
    assert lifecycle.status.actual is ActualState.STOPPED
    lifecycle.request_start()
    assert lifecycle.status.actual is ActualState.STARTING
    lifecycle.mark_running()
    assert lifecycle.status.actual is ActualState.RUNNING
    lifecycle.request_stop()
    assert lifecycle.status.actual is ActualState.STOPPING
    lifecycle.mark_stopped()
    assert lifecycle.status.actual is ActualState.STOPPED
    assert lifecycle.status.desired is DesiredState.STOP


def test_start_is_idempotent_while_starting_or_running() -> None:
    lifecycle = ServerLifecycle(DesiredState.START)
    lifecycle.request_start()
    lifecycle.request_start()
    lifecycle.mark_running()
    lifecycle.request_start()
    assert lifecycle.status.actual is ActualState.RUNNING
    assert lifecycle.status.restart_count == 0


def test_stop_from_stopped_only_updates_desired_state() -> None:
    lifecycle = ServerLifecycle(DesiredState.STOP)
    lifecycle.request_stop()
    assert lifecycle.status.actual is ActualState.STOPPED
    assert lifecycle.status.desired is DesiredState.STOP


def test_mark_running_requires_starting() -> None:
    lifecycle = ServerLifecycle(DesiredState.START)
    with pytest.raises(RuntimeError, match="outside requested start"):
        lifecycle.mark_running()


def test_mark_stopped_requires_stopping() -> None:
    lifecycle = ServerLifecycle(DesiredState.START)
    with pytest.raises(RuntimeError, match="stopped before"):
        lifecycle.mark_stopped()


def test_failure_then_recovery_with_released_resources() -> None:
    lifecycle = ServerLifecycle(DesiredState.START)
    lifecycle.request_start()
    lifecycle.mark_failure("bind failed")
    assert lifecycle.status.actual is ActualState.FAILED
    assert lifecycle.status.last_error == "bind failed"
    assert lifecycle.can_recover
    with pytest.raises(RuntimeError, match="release"):
        lifecycle.begin_recovery(resources_released=False)
    lifecycle.begin_recovery(resources_released=True)
    assert lifecycle.status.actual is ActualState.STARTING
    assert lifecycle.status.restart_count == 1
    lifecycle.mark_running()
    assert lifecycle.status.actual is ActualState.RUNNING


def test_stop_desired_never_recovers() -> None:
    lifecycle = ServerLifecycle(DesiredState.START)
    lifecycle.request_start()
    lifecycle.mark_failure("x")
    lifecycle.request_stop()
    assert not lifecycle.can_recover


def test_start_from_failed_requires_resource_release() -> None:
    lifecycle = ServerLifecycle(DesiredState.START)
    lifecycle.request_start()
    lifecycle.mark_failure("listener died")
    with pytest.raises(RuntimeError, match="resources are released"):
        lifecycle.request_start()
    lifecycle.request_start(resources_released=True)
    assert lifecycle.status.actual is ActualState.STARTING


def test_failure_requires_nonempty_message() -> None:
    lifecycle = ServerLifecycle(DesiredState.START)
    with pytest.raises(ValueError, match="empty"):
        lifecycle.mark_failure("   ")
