"""Protocol Server lifecycle state and recovery eligibility, without protocol I/O."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class DesiredState(StrEnum):
    START = "START"
    STOP = "STOP"


class ActualState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ServerStatus:
    desired: DesiredState
    actual: ActualState = ActualState.STOPPED
    restart_count: int = 0
    last_error: str = ""
    last_failure_at: datetime | None = None


class ServerLifecycle:
    """Pure transition rules; stage two owns actual library start/stop operations."""

    def __init__(self, desired: DesiredState) -> None:
        self._status = ServerStatus(desired=desired)

    @property
    def status(self) -> ServerStatus:
        return self._status

    def request_start(self, *, resources_released: bool = False) -> None:
        if self._status.actual in {ActualState.STARTING, ActualState.RUNNING}:
            return
        if self._status.actual is ActualState.STOPPING or (
            self._status.actual is ActualState.FAILED and not resources_released
        ):
            raise RuntimeError("cannot start before previous server resources are released")
        self._status = ServerStatus(
            desired=DesiredState.START,
            actual=ActualState.STARTING,
            restart_count=self._status.restart_count,
            last_error=self._status.last_error,
            last_failure_at=self._status.last_failure_at,
        )

    def mark_running(self) -> None:
        if self._status.desired is not DesiredState.START or (
            self._status.actual is not ActualState.STARTING
        ):
            raise RuntimeError("server cannot become RUNNING outside requested start")
        self._status = ServerStatus(
            desired=DesiredState.START,
            actual=ActualState.RUNNING,
            restart_count=self._status.restart_count,
            last_error=self._status.last_error,
            last_failure_at=self._status.last_failure_at,
        )

    def request_stop(self) -> None:
        if self._status.actual is ActualState.STOPPED:
            self._status = ServerStatus(
                desired=DesiredState.STOP,
                restart_count=self._status.restart_count,
                last_error=self._status.last_error,
                last_failure_at=self._status.last_failure_at,
            )
            return
        self._status = ServerStatus(
            desired=DesiredState.STOP,
            actual=ActualState.STOPPING,
            restart_count=self._status.restart_count,
            last_error=self._status.last_error,
            last_failure_at=self._status.last_failure_at,
        )

    def mark_stopped(self) -> None:
        if self._status.actual is not ActualState.STOPPING:
            raise RuntimeError("server resources must be stopped before marking STOPPED")
        self._status = ServerStatus(
            desired=self._status.desired,
            restart_count=self._status.restart_count,
            last_error=self._status.last_error,
            last_failure_at=self._status.last_failure_at,
        )

    def mark_failure(self, message: str) -> None:
        if not message.strip():
            raise ValueError("failure message cannot be empty")
        self._status = ServerStatus(
            desired=self._status.desired,
            actual=ActualState.FAILED,
            restart_count=self._status.restart_count,
            last_error=message,
            last_failure_at=datetime.now(UTC),
        )

    @property
    def can_recover(self) -> bool:
        return (
            self._status.desired is DesiredState.START and self._status.actual is ActualState.FAILED
        )

    def begin_recovery(self, *, resources_released: bool) -> None:
        if not self.can_recover or not resources_released:
            raise RuntimeError("recovery requires START and safe release of old resources")
        self._status = ServerStatus(
            desired=DesiredState.START,
            actual=ActualState.STARTING,
            restart_count=self._status.restart_count + 1,
            last_error=self._status.last_error,
            last_failure_at=self._status.last_failure_at,
        )
