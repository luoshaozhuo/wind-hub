"""Commander non-gRPC process lifecycle and operational health."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path

from commander.application.audit import AuditDelivery, AuditTransport
from commander.application.config_manager import (
    CommanderConfigManager,
    ReloadReport,
)
from commander.application.control import CommandControl
from commander.application.journal_port import JournalMetrics, JournalPort
from commander.application.persistence import BufferedJournal, PersistenceWorker
from commander.application.queued_executor import QueuedExecutor
from commander.application.settings import ServiceSettings
from core.application.protocol_registry import ProtocolRegistry

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
    audit_published: int
    audit_failures: int
    repository_used_bytes: int
    audit_pending_count: int
    audit_oldest_age_ms: float
    control_accepted_total: int
    control_terminal_counts: tuple[tuple[str, int], ...]


class CommanderService:
    """Own the journal, configuration and dispatcher in one active process.

    Starts without network I/O; a command opens its device session on demand.
    External supervisors own process restart. No implicit field-command replay.
    """

    def __init__(
        self,
        *,
        config_root: Path,
        protocol_registry: ProtocolRegistry,
        audit_transport: AuditTransport,
        settings_loader: Callable[[], ServiceSettings],
        journal_factory: Callable[[ServiceSettings], JournalPort],
    ) -> None:
        self._config_root = config_root
        self._registry = protocol_registry
        self._audit_transport = audit_transport
        self._settings_loader = settings_loader
        self._journal_factory = journal_factory
        self._audit: AuditDelivery | None = None
        self._state = ServiceState.STOPPED
        self._journal: JournalPort | None = None
        self._buffer: BufferedJournal | None = None
        self._persistence: PersistenceWorker | None = None
        self._writer: ThreadPoolExecutor | None = None
        self._journal_stats: JournalMetrics | None = None
        self._control: CommandControl | None = None
        self._executor: QueuedExecutor | None = None
        self._config: CommanderConfigManager | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._recovered = 0
        self._restart_required = False
        self._cleanup_interval = 60.0
        self._shutdown_grace = 10.0
        self._lifecycle_lock = asyncio.Lock()

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
        async with self._lifecycle_lock:
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self._state is not ServiceState.STOPPED:
            raise RuntimeError("service already started")
        self._state = ServiceState.STARTING
        journal: JournalPort | None = None
        writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="commander-sqlite")
        self._writer = writer
        loop = asyncio.get_running_loop()
        try:
            # Parse and validate the entire candidate before enabling any write.
            manager = CommanderConfigManager(
                self._config_root,
                quiesce=self._quiesce,
                settings_loader=self._settings_loader,
            )
            settings: ServiceSettings = manager.start()
            journal = await loop.run_in_executor(writer, partial(self._journal_factory, settings))
            recovered = await loop.run_in_executor(writer, journal.recover)
            initial_sequence = await loop.run_in_executor(writer, journal.latest_receive_seq)
            buffer = BufferedJournal(journal, initial_sequence=initial_sequence)
            persistence = PersistenceWorker(buffer, journal, writer)
            control = CommandControl(journal, writer, buffer)
            executor = QueuedExecutor(
                control,
                lambda device_id: manager.create_session(device_id, self._registry),
                manager.authorized,
                manager.prepare_write,
                global_capacity=settings.global_capacity,
                queue_timeout=settings.queue_timeout,
                connect_timeout=settings.connect_timeout,
                write_timeout=settings.write_timeout,
                idle_timeout=settings.idle_timeout,
                max_queued_batches_per_device=settings.max_queued_batches_per_device,
                max_queued_batches_global=settings.max_queued_batches_global,
                max_batch_points=settings.max_batch_points,
            )
            audit = AuditDelivery(
                journal,
                self._audit_transport,
                writer,
                retry_base=settings.audit_retry_base,
                retry_max=settings.audit_retry_max,
            )
            persistence.start()
            self._buffer = buffer
            self._persistence = persistence
            audit.start()
            self._audit = audit
            self._journal = journal
            self._control = control
            self._executor = executor
            self._config = manager
            self._recovered = len(recovered)
            self._journal_stats = await loop.run_in_executor(writer, journal.metrics)
            self._cleanup_interval = settings.cleanup_interval_seconds
            self._shutdown_grace = settings.shutdown_grace_seconds
            self._state = ServiceState.READY
            self._watchdog = asyncio.create_task(
                self._run_watchdog(settings.watchdog_interval),
                name="commander:watchdog",
            )
        except BaseException:
            self._state = ServiceState.FAILED
            if self._persistence is not None:
                await self._persistence.stop(timeout=5.0)
                self._persistence = None
            if self._audit is not None:
                stopped = await self._audit.stop()
                if not stopped:
                    # An in-flight publisher still owns the repository.
                    raise RuntimeError("startup failed with active audit publisher") from None
                self._audit = None
                await self._audit_transport.close()
            if journal is not None:
                await loop.run_in_executor(writer, journal.close)
            if self._writer is not None:
                await asyncio.to_thread(self._writer.shutdown, wait=True)
                self._writer = None
            raise

    async def _quiesce(self, devices: frozenset[str]) -> frozenset[str]:
        if self._executor is None:
            raise RuntimeError("no active executor")
        return await self._executor.quiesce_devices(devices)

    async def reload(self) -> ReloadReport:
        async with self._lifecycle_lock:
            return await self._reload_locked()

    async def _reload_locked(self) -> ReloadReport:
        if self._config is None or self._state not in (ServiceState.READY, ServiceState.DEGRADED):
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
        blocked = tuple(
            sorted(
                (self._executor.blocked_devices() if self._executor else frozenset())
                | (self._config.blocked_devices() if self._config else frozenset())
            )
        )
        operational = self._executor is not None and self._executor.operational
        audit_operational = self._audit is not None and self._audit.operational
        journal_operational = self._journal is not None and self._journal.operational
        stats = self._journal_stats if journal_operational else None
        return RuntimeHealth(
            state=self._state,
            ready=(
                self._state is ServiceState.READY
                and not blocked
                and operational
                and audit_operational
                and journal_operational
            ),
            revision=self._config.revision if self._config else "",
            blocked_devices=blocked,
            recovered_commands=self._recovered,
            restart_required=self._restart_required,
            pending_commands=self._executor.pending_commands() if self._executor else 0,
            inflight_io=self._executor.in_flight_native() if self._executor else 0,
            audit_published=self._audit.published if self._audit else 0,
            audit_failures=self._audit.failures if self._audit else 0,
            repository_used_bytes=stats.used_bytes if stats else 0,
            audit_pending_count=(stats.pending_events if stats else 0)
            + (self._buffer.pending_count if self._buffer else 0),
            audit_oldest_age_ms=stats.oldest_pending_age_ms if stats else 0.0,
            control_accepted_total=stats.accepted_total if stats else 0,
            control_terminal_counts=stats.terminal_counts if stats else (),
        )

    async def _run_watchdog(self, interval: float) -> None:
        elapsed = 0.0
        while self._state in (ServiceState.READY, ServiceState.DEGRADED):
            await asyncio.sleep(interval)
            elapsed += interval
            if elapsed >= self._cleanup_interval and self._journal is not None:
                elapsed = 0.0
                try:
                    if self._writer is not None:
                        await asyncio.get_running_loop().run_in_executor(
                            self._writer, self._journal.cleanup
                        )
                except Exception:
                    logger.exception("commander journal cleanup failed")
            if self._journal is not None and self._writer is not None:
                try:
                    self._journal_stats = await asyncio.get_running_loop().run_in_executor(
                        self._writer, self._journal.metrics
                    )
                except Exception:
                    self._state = ServiceState.FAILED
                    logger.exception("Commander journal metrics unavailable")
                    return
            if self._persistence is not None and self._persistence.failure is not None:
                self._state = ServiceState.DEGRADED
                logger.warning("Commander persistence retrying", exc_info=self._persistence.failure)
            if self._executor is None:
                return
            blocked = self._executor.blocked_devices()
            if self._config is not None:
                blocked |= self._config.blocked_devices()
            if not self._executor.operational or (
                self._journal is not None and not self._journal.operational
            ):
                self._state = ServiceState.FAILED
                logger.error("Commander dispatcher unavailable")
                return
            if self._audit is None or not self._audit.operational:
                self._state = ServiceState.DEGRADED
                logger.error("Commander audit delivery stopped unexpectedly")
            elif blocked:
                self._state = ServiceState.DEGRADED
                logger.warning("commander blocked devices", extra={"count": len(blocked)})
            elif self._state is ServiceState.DEGRADED:
                self._state = ServiceState.READY

    async def stop(self) -> frozenset[str]:
        async with self._lifecycle_lock:
            return await self._stop_locked()

    async def _stop_locked(self) -> frozenset[str]:
        if self._state is ServiceState.STOPPED:
            return frozenset()
        self._state = ServiceState.STOPPING
        if self._watchdog is not None:
            self._watchdog.cancel()
            await asyncio.gather(self._watchdog, return_exceptions=True)
            self._watchdog = None
        blocked: frozenset[str] = frozenset()
        if self._executor is not None:
            blocked = await self._executor.close(grace_seconds=self._shutdown_grace)
        # Unsafe resources must never be hidden by a reported clean stop.
        if blocked:
            self._state = ServiceState.FAILED
            return blocked
        if self._persistence is not None:
            persisted = await self._persistence.stop(timeout=self._shutdown_grace)
            if not persisted:
                self._state = ServiceState.FAILED
                return frozenset({"__persistence_queue__"})
            self._persistence = None
        if self._audit is not None:
            audit_settled = await self._audit.stop(timeout=self._shutdown_grace)
            if not audit_settled:
                self._state = ServiceState.FAILED
                return frozenset({"__audit_publisher__"})
            self._audit = None
        try:
            await self._audit_transport.close()
        except Exception:
            self._state = ServiceState.FAILED
            return frozenset({"__audit_connection__"})
        if self._journal is not None and self._writer is not None:
            await asyncio.get_running_loop().run_in_executor(self._writer, self._journal.close)
            self._journal = None
        if self._writer is not None:
            await asyncio.to_thread(self._writer.shutdown, wait=True)
            self._writer = None
        if self._buffer is not None:
            self._buffer.close()
            self._buffer = None
        self._state = ServiceState.STOPPED
        return blocked
