"""Commander non-gRPC process lifecycle and operational health."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from commander.application.config_manager import (
    CommanderConfigManager,
    ReloadReport,
)
from commander.application.control import CommandControl
from commander.application.queued_executor import QueuedExecutor, SessionFactory
from commander.infrastructure.event_repository import EventRepository
from commander.infrastructure.service_settings import ServiceSettings

logger = logging.getLogger(__name__)


class ServiceState(StrEnum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    READY = "READY"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    state: ServiceState
    ready: bool
    revision: str
    blocked_devices: tuple[str, ...]
    recovered_commands: int
    restart_required: bool
    pending_commands: int
    inflight_io: int


class CommanderService:
    """Own the journal, configuration and dispatcher in one active process.

    Starts without network I/O; a command opens its device session on demand.
    External supervisors own process restart. No implicit field-command replay.
    """

    def __init__(
        self,
        *,
        config_root: Path,
        service_config: Path,
        session_factory: SessionFactory,
    ) -> None:
        self._config_root = config_root
        self._service_path = service_config
        self._factory = session_factory
        self._state = ServiceState.STOPPED
        self._journal: EventRepository | None = None
        self._control: CommandControl | None = None
        self._executor: QueuedExecutor | None = None
        self._config: CommanderConfigManager | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._recovered = 0
        self._restart_required = False
        self._cleanup_interval = 60.0

    @property
    def control(self) -> CommandControl:
        if self._state not in (ServiceState.READY, ServiceState.DEGRADED):
            raise RuntimeError("service is not ready")
        if self._control is None:
            raise RuntimeError("command journal not initialized")
        return self._control

    @property
    def executor(self) -> QueuedExecutor:
        if self._state not in (ServiceState.READY, ServiceState.DEGRADED):
            raise RuntimeError("service is not ready")
        if self._executor is None:
            raise RuntimeError("executor is not initialized")
        return self._executor

    async def start(self) -> None:
        if self._state is not ServiceState.STOPPED:
            raise RuntimeError("service already started")
        self._state = ServiceState.STARTING
        journal: EventRepository | None = None
        try:
            # Parse and validate the entire candidate before enabling any write.
            manager = CommanderConfigManager(
                self._config_root,
                self._service_path,
                quiesce=self._quiesce,
            )
            settings: ServiceSettings = manager.start()
            journal = EventRepository(
                settings.sqlite_path,
                max_storage_bytes=settings.max_storage_bytes,
                reserved_storage_bytes=settings.reserved_storage_bytes,
                admission_stop_ratio=settings.admission_stop_ratio,
                retention_seconds=settings.retention_seconds,
            )
            recovered = journal.recover()
            control = CommandControl(journal)
            executor = QueuedExecutor(
                control,
                self._factory,
                manager.authorized,
                global_capacity=settings.global_capacity,
                queue_timeout=settings.queue_timeout,
                connect_timeout=settings.connect_timeout,
                write_timeout=settings.write_timeout,
                idle_timeout=settings.idle_timeout,
                max_queued_batches_per_device=settings.max_queued_batches_per_device,
                max_queued_batches_global=settings.max_queued_batches_global,
                max_batch_points=settings.max_batch_points,
            )
            self._journal = journal
            self._control = control
            self._executor = executor
            self._config = manager
            self._recovered = len(recovered)
            self._cleanup_interval = settings.cleanup_interval_seconds
            self._state = ServiceState.READY
            self._watchdog = asyncio.create_task(
                self._run_watchdog(settings.watchdog_interval),
                name="commander:watchdog",
            )
        except BaseException:
            self._state = ServiceState.FAILED
            if journal is not None:
                journal.close()
            raise

    async def _quiesce(self, devices: frozenset[str]) -> frozenset[str]:
        if self._executor is None:
            raise RuntimeError("no active executor")
        return await self._executor.quiesce_devices(devices)

    async def reload(self) -> ReloadReport:
        if self._config is None or self._state not in (
            ServiceState.READY, ServiceState.DEGRADED
        ):
            raise RuntimeError("service not ready")
        report = await self._config.reload()
        self._restart_required = report.restart_required
        self._state = (
            ServiceState.DEGRADED
            if report.blocked or (self._executor and self._executor.blocked_devices())
            else ServiceState.READY
        )
        return report

    def health(self) -> RuntimeHealth:
        blocked = (
            tuple(sorted(self._executor.blocked_devices()))
            if self._executor is not None
            else ()
        )
        operational = self._executor is not None and self._executor.operational
        return RuntimeHealth(
            state=self._state,
            ready=self._state is ServiceState.READY and not blocked and operational,
            revision=self._config.revision if self._config else "",
            blocked_devices=blocked,
            recovered_commands=self._recovered,
            restart_required=self._restart_required,
            pending_commands=self._executor.pending_commands() if self._executor else 0,
            inflight_io=self._executor.in_flight_native() if self._executor else 0,
        )

    async def _run_watchdog(self, interval: float) -> None:
        elapsed = 0.0
        while self._state in (ServiceState.READY, ServiceState.DEGRADED):
            await asyncio.sleep(interval)
            elapsed += interval
            if elapsed >= self._cleanup_interval and self._journal is not None:
                elapsed = 0.0
                try:
                    self._journal.cleanup()
                except Exception:
                    logger.exception("commander journal cleanup failed")
            if self._executor is None:
                return
            blocked = self._executor.blocked_devices()
            if not self._executor.operational:
                self._state = ServiceState.FAILED
                logger.error("Commander dispatcher unavailable")
                return
            if blocked:
                self._state = ServiceState.DEGRADED
                logger.warning("commander blocked devices", extra={"count": len(blocked)})
            elif self._state is ServiceState.DEGRADED:
                self._state = ServiceState.READY

    async def stop(self) -> frozenset[str]:
        if self._state is ServiceState.STOPPED:
            return frozenset()
        self._state = ServiceState.STOPPING
        if self._watchdog is not None:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
            self._watchdog = None
        blocked = frozenset()
        if self._executor is not None:
            blocked = await self._executor.close()
        # Unsafe resources must never be hidden by a reported clean stop.
        if blocked:
            self._state = ServiceState.FAILED
            return blocked
        if self._journal is not None:
            self._journal.close()
        self._state = ServiceState.STOPPED
        return blocked
