"""Commander non-gRPC process lifecycle and operational health."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import partial
from uuid import uuid4

from commander.application.audit import AuditDelivery, AuditTransport
from commander.application.config_manager import (
    CommanderConfigManager,
    ReloadReport,
)
from commander.application.control import CommandControl
from commander.application.metrics import CommandMetrics
from commander.application.persistence import BufferedJournal, PersistenceWorker
from commander.application.ports.journal import JournalMetrics, JournalPort
from commander.application.ports.recovery import RecoveryAuthority
from commander.application.queued_executor import QueuedExecutor, WriteSession
from commander.application.settings import ServiceSettings, SimulationGatewayTarget
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
    persistence_operational: bool
    audit_operational: bool
    journal_operational: bool
    dispatcher_operational: bool


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: str
    timestamp: datetime
    severity: str
    source: str
    error_code: str
    message: str


class CommanderService:
    """Own the journal, configuration and dispatcher in one active process.

    Starts without network I/O; a command opens its device session on demand.
    External supervisors own process restart. No implicit field-command replay.
    """

    def __init__(
        self,
        *,
        protocol_registry: ProtocolRegistry,
        audit_transport: AuditTransport,
        settings_loader: Callable[[], ServiceSettings],
        journal_factory: Callable[[ServiceSettings], JournalPort],
        simulation_factory: Callable[[str, SimulationGatewayTarget], WriteSession] | None = None,
    ) -> None:
        self._registry = protocol_registry
        self._audit_transport = audit_transport
        self._settings_loader = settings_loader
        self._journal_factory = journal_factory
        self._simulation_factory = simulation_factory
        self._audit: AuditDelivery | None = None
        self._state = ServiceState.STOPPED
        self._started_at: datetime | None = None
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
        self._storage_admission_limit = 0.0
        self._shutdown_grace = 10.0
        self._lifecycle_lock = asyncio.Lock()
        self._metrics = CommandMetrics()
        self._events: deque[RuntimeEvent] = deque(maxlen=1000)

    def _event(self, severity: str, message: str, error_code: str = "") -> None:
        self._events.append(
            RuntimeEvent(
                event_id=str(uuid4()),
                timestamp=datetime.now(UTC),
                severity=severity,
                source="commander",
                error_code=error_code,
                message=message,
            )
        )

    def events(self) -> tuple[RuntimeEvent, ...]:
        return tuple(self._events)

    @property
    def started_at(self) -> datetime | None:
        return self._started_at

    @property
    def metrics(self) -> CommandMetrics:
        return self._metrics

    @property
    def supported_protocols(self) -> tuple[str, ...]:
        return self._registry.registered_names()

    @property
    def config_manager(self) -> CommanderConfigManager:
        if self._config is None:
            raise RuntimeError("config manager unavailable")
        return self._config

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
                quiesce=self._quiesce,
                settings_loader=self._settings_loader,
            )
            settings: ServiceSettings = manager.start()
            journal = await loop.run_in_executor(writer, partial(self._journal_factory, settings))
            recovered = await loop.run_in_executor(writer, journal.recover)

            def recovered_device_ids() -> frozenset[str]:
                devices: set[str] = set()
                for command_id in recovered:
                    result = journal.get(command_id)
                    if result is None:
                        raise RuntimeError("recovered command is missing from durable journal")
                    devices.add(result.command.spec.device_id)
                return frozenset(devices)

            recovered_devices = await loop.run_in_executor(writer, recovered_device_ids)
            recovered_devices |= await loop.run_in_executor(
                writer, journal.recovery_blocked_devices
            )
            initial_sequence = await loop.run_in_executor(writer, journal.latest_receive_seq)
            buffer = BufferedJournal(
                initial_sequence=initial_sequence,
                maximum_events=settings.persistence_max_events,
                metrics=self._metrics,
            )
            persistence = PersistenceWorker(
                buffer,
                journal,
                writer,
                interval=settings.persistence_interval_seconds,
                batch_size=settings.persistence_batch_size,
            )
            control = CommandControl(
                journal, writer, buffer, accept_timeout=settings.accept_commit_timeout
            )

            def create_session(device_id: str) -> WriteSession:
                target = settings.simulation_targets.get(device_id)
                if target is None:
                    return manager.create_session(device_id, self._registry)
                if self._simulation_factory is None:
                    raise RuntimeError("simulation execution Adapter is not registered")
                return self._simulation_factory(device_id, target)

            executor = QueuedExecutor(
                control,
                create_session,
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
                max_request_id_bytes=settings.max_request_id_bytes,
                initially_blocked=recovered_devices,
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
            self._storage_admission_limit = min(
                settings.max_storage_bytes * settings.admission_stop_ratio,
                settings.max_storage_bytes - settings.reserved_storage_bytes,
            )
            executor.set_storage_admission(
                self._journal_stats.used_bytes < self._storage_admission_limit
            )
            self._cleanup_interval = settings.cleanup_interval_seconds
            self._shutdown_grace = settings.shutdown_grace_seconds
            # Recovered UNKNOWN commands may have reached the field. Refuse
            # fresh writes to those devices until isolation is explicitly resolved.
            self._state = ServiceState.DEGRADED if recovered_devices else ServiceState.READY
            self._started_at = datetime.now(UTC)
            if recovered_devices:
                self._event("WARNING", "Commander recovered uncertain devices", "RECOVERY_BLOCKED")
            else:
                self._event("INFO", "Commander ready")
            self._watchdog = asyncio.create_task(
                self._run_watchdog(settings.watchdog_interval),
                name="commander:watchdog",
            )
        except BaseException:
            self._state = ServiceState.FAILED
            self._event("ERROR", "Commander startup failed", "STARTUP_FAILED")
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

    async def recover_device_control(
        self,
        *,
        authority: RecoveryAuthority | None,
        caller_id: str,
        device_id: str,
        expected_revision: str,
        request_id: str,
        attestation: bytes,
    ) -> tuple[str, str]:
        """Fence externally, commit durable audit, then release one recovered block."""
        if authority is None:
            raise RuntimeError("independent isolation attestation verifier is not configured")
        async with self._lifecycle_lock:
            if self._state not in (ServiceState.READY, ServiceState.DEGRADED):
                raise RuntimeError("Commander not ready for recovery")
            manager = self.config_manager
            executor = self.executor
            revision = manager.device_revision(device_id)
            if revision is None or revision != expected_revision:
                raise RuntimeError("device configuration revision conflict")
            journal = self._journal
            writer = self._writer
            if (
                journal is None
                or writer is None
                or not journal.operational
                or self._audit is None
                or not self._audit.operational
            ):
                raise RuntimeError("durable recovery audit unavailable")
            digest = hashlib.sha256(attestation).hexdigest()
            loop = asyncio.get_running_loop()
            previous = await loop.run_in_executor(
                writer, journal.recovery_decision, caller_id, request_id
            )
            if previous is not None and (
                previous[1] != device_id or previous[2] != revision or previous[3] != digest
            ):
                raise FileExistsError("recovery request_id conflict")
            blocked = device_id in executor.blocked_devices()
            if not blocked and previous is None:
                raise RuntimeError("device does not require recovery")
            # Even an idempotent retry must confirm that fencing remains live.
            proof = await authority.verify_and_fence(
                device_id=device_id,
                revision=revision,
                caller_id=caller_id,
                request_id=request_id,
                attestation=attestation,
            )
            if self._state not in (ServiceState.READY, ServiceState.DEGRADED):
                raise RuntimeError("Commander stopped during recovery")
            if not blocked:
                # A past idempotent decision cannot authorize an older owner
                # after a newer fencing epoch has been durably committed.
                latest_epoch = await loop.run_in_executor(
                    writer, journal.latest_recovery_epoch, device_id
                )
                if (
                    previous is None
                    or previous[4] < latest_epoch
                    or previous[5] != proof.token_id
                    or proof.fencing_epoch < latest_epoch
                    or proof.expires_at <= datetime.now(UTC)
                    or self._audit is None
                    or not self._audit.operational
                ):
                    raise RuntimeError("no valid current recovery decision")
                return previous[0], digest
            if manager.device_revision(device_id) != revision:
                raise RuntimeError("device revision changed during recovery")
            if self._audit is None or not self._audit.operational:
                raise RuntimeError("recovery audit worker stopped")
            decision_id = await loop.run_in_executor(
                writer,
                partial(
                    journal.commit_recovery,
                    caller_id=caller_id,
                    request_id=request_id,
                    device_id=device_id,
                    revision=revision,
                    evidence_digest=digest,
                    token_id=proof.token_id,
                    fencing_epoch=proof.fencing_epoch,
                    old_instance=proof.old_instance,
                    new_instance=proof.new_instance,
                ),
            )
            # The SQLite commit is not evidence that physical fencing remains
            # effective. Re-check the live authority *after* commit, while the
            # lifecycle lock prevents an overlapping reload or stop.
            current_proof = await authority.verify_and_fence(
                device_id=device_id,
                revision=revision,
                caller_id=caller_id,
                request_id=request_id,
                attestation=attestation,
            )
            if (
                current_proof.device_id != proof.device_id
                or current_proof.revision != proof.revision
                or current_proof.old_instance != proof.old_instance
                or current_proof.new_instance != proof.new_instance
                or current_proof.token_id != proof.token_id
                or current_proof.fencing_epoch < proof.fencing_epoch
                or current_proof.expires_at <= datetime.now(UTC)
                or self._audit is None
                or not self._audit.operational
                or not journal.operational
                or manager.device_revision(device_id) != revision
            ):
                raise RuntimeError("recovery fencing or audit changed before release")
            # A persisted decision followed by a failed verification remains
            # blocked; repeat recovery requires fresh external fencing proof.
            executor.release_recovered_device(device_id)
            self._event("INFO", "Commander device recovery committed", "RECOVERY_UNBLOCKED")
            return decision_id, digest

    async def reload(self) -> ReloadReport:
        async with self._lifecycle_lock:
            return await self._reload_locked()

    async def _reload_locked(self) -> ReloadReport:
        if self._config is None or self._state not in (ServiceState.READY, ServiceState.DEGRADED):
            raise RuntimeError("service not ready")
        report = await self._config.reload()
        self._restart_required = report.restart_required
        self._event("INFO", f"Config applied: {report.revision}")
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
                and self._executor is not None
                and self._executor.accepting_commands
                and audit_operational
                and journal_operational
                and (
                    self._persistence is not None
                    and self._persistence.operational
                    and self._persistence.failure is None
                )
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
            persistence_operational=(
                self._persistence is not None
                and self._persistence.operational
                and self._persistence.failure is None
            ),
            audit_operational=audit_operational,
            journal_operational=journal_operational,
            dispatcher_operational=operational,
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
                    if self._executor is not None:
                        self._executor.set_storage_admission(
                            self._journal_stats.used_bytes < self._storage_admission_limit
                        )
                except Exception:
                    self._state = ServiceState.DEGRADED
                    logger.exception("Commander journal metrics unavailable")
            if self._persistence is not None and not self._persistence.operational:
                self._state = ServiceState.DEGRADED
                logger.error("Commander persistence worker stopped unexpectedly")
            elif self._persistence is not None and self._persistence.failure is not None:
                self._state = ServiceState.DEGRADED
                logger.warning("Commander persistence retrying", exc_info=self._persistence.failure)
            if self._executor is None:
                return
            blocked = self._executor.blocked_devices()
            if self._config is not None:
                blocked |= self._config.blocked_devices()
            if not self._executor.operational:
                self._state = ServiceState.FAILED
                logger.error("Commander dispatcher unavailable")
                return
            if self._journal is not None and not self._journal.operational:
                self._state = ServiceState.DEGRADED
                logger.error("Commander SQLite journal unavailable")
            if self._audit is None or not self._audit.operational:
                self._state = ServiceState.DEGRADED
                logger.error("Commander audit delivery stopped unexpectedly")
            elif (
                blocked
                or not self._executor.accepting_commands
                or (
                    self._persistence is not None
                    and (not self._persistence.operational or self._persistence.failure is not None)
                )
                or (self._journal is not None and not self._journal.operational)
            ):
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
        self._event("INFO", "Commander stopped")
        return blocked
