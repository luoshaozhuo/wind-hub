"""Per-device FIFO dispatcher with durable decisions and safe native I/O ownership."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from commander.application.control import CommandControl
from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
    request_fingerprint,
)
from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)


class WriteSession(Protocol):
    """One device transport. Factory construction must not perform native I/O."""

    async def connect(self) -> None: ...

    def is_open(self) -> bool: ...

    def capabilities(self) -> frozenset[ProtocolCapability]: ...

    async def write_one(self, write: ProtocolWrite) -> ProtocolWriteResult: ...

    async def write_many(
        self, writes: Sequence[ProtocolWrite]
    ) -> tuple[ProtocolWriteResult, ...]: ...

    async def close(self) -> None: ...


@runtime_checkable
class AuthorizedWriteSession(Protocol):
    """Optional command-aware Port preserving the durable per-command identity."""

    async def write_authorized(self, command: AcceptedCommand) -> ProtocolWriteResult: ...


SessionFactory = Callable[[str], WriteSession]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entry:
    command: AcceptedCommand
    deadline: float
    finished: asyncio.Future[None]
    terminal_in_queue: bool = False
    gate_acquired: bool = False
    wakeup: asyncio.Event | None = None
    deadline_handle: asyncio.TimerHandle | None = None


class _SerialWriteError(Exception):
    def __init__(
        self,
        results: tuple[ProtocolWriteResult, ...],
        cause: Exception,
        *,
        current_unconfirmed: bool,
    ) -> None:
        super().__init__(str(cause))
        self.results = results
        self.cause = cause
        self.current_unconfirmed = current_unconfirmed


class QueuedExecutor:
    """Serialize each device and count native connect/write against a global gate.

    A timeout detaches only the caller, never the native task. The device stays
    blocked after ambiguous I/O, while global capacity is released only when
    the native task actually settles. Operators must isolate blocked devices
    before a new session can be installed.
    """

    def __init__(
        self,
        control: CommandControl,
        session_factory: SessionFactory,
        authorize: Callable[[CommandSpec], bool],
        prepare_write: Callable[[CommandSpec], ProtocolWrite],
        *,
        global_capacity: int = 32,
        queue_timeout: float = 5.0,
        connect_timeout: float = 3.0,
        write_timeout: float = 3.0,
        max_queued_batches_per_device: int = 10,
        max_queued_batches_global: int = 1000,
        idle_timeout: float = 30.0,
        max_batch_points: int = 10,
        max_request_id_bytes: int = 128,
        initially_blocked: frozenset[str] = frozenset(),
    ) -> None:
        if (
            global_capacity < 1
            or max_queued_batches_per_device < 1
            or max_queued_batches_global < 1
            or max_batch_points < 1
            or max_request_id_bytes < 1
        ):
            raise ValueError("capacities must be positive")
        if min(queue_timeout, connect_timeout, write_timeout, idle_timeout) <= 0:
            raise ValueError("timeouts must be positive")
        self._control = control
        self._admission_lock = asyncio.Lock()
        self._admissions: set[asyncio.Task[tuple[AcceptedCommand, ...]]] = set()
        self._factory = session_factory
        self._authorize = authorize
        self._prepare_write = prepare_write
        self._gate = asyncio.Semaphore(global_capacity)
        self._queue_timeout = queue_timeout
        self._connect_timeout = connect_timeout
        self._write_timeout = write_timeout
        self._device_batch_limit = max_queued_batches_per_device
        self._global_batch_limit = max_queued_batches_global
        self._idle_timeout = idle_timeout
        self._max_batch_points = max_batch_points
        self._max_request_id_bytes = max_request_id_bytes
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}
        self._deadline_tasks: set[asyncio.Task[None]] = set()
        self._idle_closing: set[str] = set()
        self._queues: dict[str, deque[_Entry]] = defaultdict(deque)
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._entries: dict[str, _Entry] = {}
        self._sessions: dict[str, WriteSession] = {}
        self._blocked: set[str] = set(initially_blocked)
        self._reloading: set[str] = set()
        self._native: set[asyncio.Task[Any]] = set()
        self._native_by_device: dict[str, set[asyncio.Task[Any]]] = defaultdict(set)
        self._isolation_tasks: dict[str, asyncio.Task[None]] = {}
        self._retained_permits: dict[str, int] = defaultdict(int)
        self._pending_unknown: dict[str, set[str]] = defaultdict(set)
        self._settled_unknown: set[str] = set()
        self._settled_unknown_order: deque[str] = deque()
        self._auto_recovery: set[asyncio.Task[bool]] = set()
        self._closed = False
        self._fatal = False
        self._storage_admission_open = True

    async def accept(
        self,
        commands: Sequence[CommandSpec],
        *,
        caller_id: str,
        request_id: str,
    ) -> tuple[AcceptedCommand, ...]:
        """Do not revoke an in-progress admission on gRPC cancellation."""
        operation = asyncio.create_task(
            self._accept_persist_and_queue(commands, caller_id=caller_id, request_id=request_id),
            name="commander:request-admission",
        )
        self._admissions.add(operation)
        operation.add_done_callback(self._admission_completed)
        return await asyncio.shield(operation)

    def _admission_completed(self, task: asyncio.Task[tuple[AcceptedCommand, ...]]) -> None:
        self._admissions.discard(task)
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            logger.error("Commander request admission failed: %s", failure)

    async def _accept_persist_and_queue(
        self,
        commands: Sequence[CommandSpec],
        *,
        caller_id: str,
        request_id: str,
    ) -> tuple[AcceptedCommand, ...]:
        """Complete durable T1 and FIFO registration as one owned operation."""
        if not request_id.strip() or len(request_id.encode("utf-8")) > self._max_request_id_bytes:
            raise ValueError("request_id is empty or exceeds configured byte limit")
        async with self._admission_lock:
            previous = await self._control.get_request(request_id, caller_id)
            if previous:
                if request_fingerprint([item.command.spec for item in previous]) != (
                    request_fingerprint(commands)
                ):
                    raise FileExistsError("request_id already belongs to different commands")
                return tuple(result.command for result in previous)
            if self._closed or self._fatal:
                raise RuntimeError("dispatcher unavailable")
            if not self._storage_admission_open:
                raise OverflowError("SQLite admission capacity exhausted")
            if len(commands) > self._max_batch_points:
                raise OverflowError("batch point limit exceeded")
            counts: dict[str, int] = defaultdict(int)
            for spec in commands:
                if spec.expires_at is not None and spec.expires_at <= datetime.now(UTC):
                    raise ValueError("expires_at is in the past")
                if spec.device_id in self._reloading or spec.device_id in self._blocked:
                    raise RuntimeError("device unavailable for control")
                if not self._authorize(spec):
                    raise PermissionError("command device/point/config reference is not authorized")
                # Reject invalid values before admitting them to the in-memory journal.
                self._prepare_write(spec)
                counts[spec.device_id] += 1
            for device_id in counts:
                pending = {item.command.request_id for item in self._queues[device_id]}
                if len(pending) >= self._device_batch_limit:
                    raise OverflowError("per-device pending batch capacity exceeded")
            pending_global = {
                item.command.request_id for queue in self._queues.values() for item in queue
            }
            if len(pending_global) >= self._global_batch_limit:
                raise OverflowError("global pending batch capacity exceeded")
            loop = asyncio.get_running_loop()
            started = time.monotonic()
            # Commit all T1 events durably before registering device workers.
            # The shielded admission task continues even if its RPC is cancelled.
            accepted = await self._control.accept(
                commands, caller_id=caller_id, request_id=request_id
            )
            for command in accepted:
                entry = _Entry(
                    command=command,
                    deadline=started + self._queue_timeout,
                    finished=loop.create_future(),
                    wakeup=asyncio.Event(),
                )
                entry.deadline_handle = loop.call_at(entry.deadline, self._schedule_expiry, entry)
                self._entries[command.command_id] = entry
                self._queues[command.spec.device_id].append(entry)
            for device_id in counts:
                self._ensure_worker(device_id)
            return accepted

    async def _terminate_queued(self, entry: _Entry, state: CommandState, code: str) -> None:
        if entry.finished.done() or entry.terminal_in_queue:
            return
        # Claim the queued entry before finalizing its in-memory state.
        entry.terminal_in_queue = True
        try:
            await self._unsent(entry.command.command_id, state, code)
        except Exception as exc:
            self._fail_waiters(exc)
            raise
        if entry.wakeup is not None:
            entry.wakeup.set()
        queue = self._queues[entry.command.spec.device_id]
        if queue and queue[0] is not entry and entry in queue:
            queue.remove(entry)
            if entry.deadline_handle is not None:
                entry.deadline_handle.cancel()
            self._entries.pop(entry.command.command_id, None)
            entry.finished.set_result(None)

    def _schedule_expiry(self, entry: _Entry) -> None:
        task = asyncio.create_task(self._expire_queued(entry))
        self._deadline_tasks.add(task)
        task.add_done_callback(self._deadline_tasks.discard)

    async def _expire_queued(self, entry: _Entry) -> None:
        if entry.finished.done() or entry.terminal_in_queue or entry.gate_acquired:
            return
        try:
            state = await self._control.get_command(entry.command.command_id)
            if entry.gate_acquired or state is None or state.state is not CommandState.ACCEPTED:
                return
            await self._terminate_queued(entry, CommandState.EXPIRED, "QUEUE_TIMEOUT")
        except Exception as exc:
            self._fail_waiters(exc)

    async def wait(self, command_id: str) -> CommandResult | None:
        entry = self._entries.get(command_id)
        if entry is not None:
            await asyncio.shield(entry.finished)
        return await self._control.get_command(command_id)

    async def quiesce_devices(self, device_ids: frozenset[str]) -> frozenset[str]:
        """Stop admissions and writes for changed devices until reload finishes."""
        self._reloading.update(device_ids)
        failed: set[str] = set()
        try:
            for device_id in sorted(device_ids):
                worker = self._workers.get(device_id)
                if worker is not None and not worker.done():
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(worker),
                            timeout=self._queue_timeout
                            + self._connect_timeout
                            + self._write_timeout,
                        )
                    except TimeoutError:
                        self._blocked.add(device_id)
                        failed.add(device_id)
                        continue
                if device_id in self._blocked:
                    if not await self.isolate_device(device_id):
                        failed.add(device_id)
                    continue
                session = self._sessions.get(device_id)
                if session is not None:
                    closing: asyncio.Task[None] | None = self._isolation_tasks.get(device_id)
                    if closing is None:
                        closing = asyncio.create_task(session.close())
                        self._isolation_tasks[device_id] = closing
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(closing),
                            timeout=self._connect_timeout,
                        )
                    except Exception:
                        self._blocked.add(device_id)
                        failed.add(device_id)
                    else:
                        self._isolation_tasks.pop(device_id, None)
                        self._sessions.pop(device_id, None)
            return frozenset(failed)
        finally:
            self._reloading.difference_update(device_ids)

    def set_storage_admission(self, open_for_new_commands: bool) -> None:
        """Close new admissions at storage stop threshold; do not cancel accepted I/O."""
        self._storage_admission_open = open_for_new_commands

    @property
    def accepting_commands(self) -> bool:
        return self.operational and self._storage_admission_open

    @property
    def operational(self) -> bool:
        return not self._closed and not self._fatal

    def pending_commands(self) -> int:
        return sum(len(queue) for queue in self._queues.values())

    def device_queue_depth(self, device_id: str) -> int:
        return len(self._queues.get(device_id, ()))

    def in_flight_native(self) -> int:
        return len(self._native) + sum(self._retained_permits.values())

    def blocked_devices(self) -> frozenset[str]:
        return frozenset(self._blocked)

    def inspect_existing_session(self, device_id: str) -> tuple[str, tuple[str, ...]]:
        """Read-only protocol capability observation; never create or poll a session."""
        if device_id in self._blocked:
            return "BLOCKED", ()
        session = self._sessions.get(device_id)
        if session is None:
            return "NOT_CONNECTED", ()
        if device_id in self._idle_closing or device_id in self._isolation_tasks:
            return "CLOSING", ()
        return (
            "CONNECTED" if session.is_open() else "DISCONNECTED",
            tuple(sorted(capability.value for capability in session.capabilities())),
        )

    def io_settled(self, command_id: str, state: CommandState, error_code: str | None) -> bool:
        """Use positive isolation evidence for uncertain write/connect I/O."""
        if state is CommandState.UNKNOWN or error_code in {"CONNECT_TIMEOUT", "CONNECT_FAILED"}:
            return command_id in self._settled_unknown
        return state in (
            CommandState.SUCCEEDED,
            CommandState.FAILED,
            CommandState.INTERRUPTED,
            CommandState.EXPIRED,
            CommandState.CANCELLED,
        )

    def _track_unknown(
        self,
        device_id: str,
        changes: tuple[tuple[str, CommandState, CommandOutcome, str | None], ...],
    ) -> None:
        for command_id, state, _, _ in changes:
            if state is CommandState.UNKNOWN:
                self._pending_unknown[device_id].add(command_id)

    def _record_io_settled(self, command_id: str) -> None:
        if command_id not in self._settled_unknown:
            self._settled_unknown.add(command_id)
            self._settled_unknown_order.append(command_id)
        # Eviction loses evidence conservatively; it cannot create false success.
        while len(self._settled_unknown_order) > 100_000:
            expired = self._settled_unknown_order.popleft()
            self._settled_unknown.discard(expired)

    def _confirm_unknown_settled(self, device_id: str) -> None:
        for command_id in self._pending_unknown.pop(device_id, set()):
            self._record_io_settled(command_id)

    def _ensure_worker(self, device_id: str) -> None:
        task = self._workers.get(device_id)
        if task is None or task.done():
            self._workers[device_id] = asyncio.create_task(
                self._run_device(device_id), name=f"commander:{device_id}"
            )

    def _fail_waiters(self, error: Exception) -> None:
        """Never strand accepted callers when a command decision fails."""
        self._fatal = True
        for item in tuple(self._entries.values()):
            if item.deadline_handle is not None:
                item.deadline_handle.cancel()
            if item.wakeup is not None:
                item.wakeup.set()
            if not item.finished.done():
                item.finished.set_exception(error)

    async def _run_device(self, device_id: str) -> None:
        idle = self._idle_tasks.pop(device_id, None)
        if idle is not None:
            if device_id not in self._idle_closing:
                idle.cancel()
            await asyncio.gather(idle, return_exceptions=True)
        queue = self._queues[device_id]
        while queue:
            if self._fatal:
                return
            entry = queue[0]
            try:
                await self._execute(device_id, entry)
            except asyncio.CancelledError:
                self._fail_waiters(RuntimeError("device worker cancelled during command"))
                raise
            except Exception as exc:
                self._fail_waiters(exc)
                return
            else:
                if not entry.finished.done():
                    entry.finished.set_result(None)
            finally:
                if queue and queue[0] is entry:
                    queue.popleft()
                if entry.deadline_handle is not None:
                    entry.deadline_handle.cancel()
                if entry.finished.done():
                    self._entries.pop(entry.command.command_id, None)
        if (
            not self._closed
            and not self._fatal
            and device_id in self._sessions
            and device_id not in self._blocked
        ):
            self._idle_tasks[device_id] = asyncio.create_task(
                self._expire_session(device_id),
                name=f"commander:idle:{device_id}",
            )

    async def _unsent(self, command_id: str, state: CommandState, code: str) -> None:
        await self._control.complete(
            command_id,
            state,
            outcome=(
                CommandOutcome.CONFIRMED_FAILURE
                if state is CommandState.FAILED
                else CommandOutcome.NOT_SENT
            ),
            error_code=code,
        )

    async def _check_before_write(self, entry: _Entry, *, queued: bool) -> bool:
        if self._fatal or entry.terminal_in_queue:
            return False
        if self._closed:
            await self._terminate_queued(entry, CommandState.INTERRUPTED, "SERVICE_STOPPING")
            return False
        if entry.command.spec.device_id in self._reloading:
            await self._terminate_queued(entry, CommandState.FAILED, "CONFIG_RELOADING")
            return False
        if queued and entry.deadline <= time.monotonic():
            await self._terminate_queued(entry, CommandState.EXPIRED, "QUEUE_TIMEOUT")
            return False
        if not self._authorize(entry.command.spec):
            await self._terminate_queued(entry, CommandState.FAILED, "CONFIG_CHANGED_OR_REVOKED")
            return False
        expires = entry.command.spec.expires_at
        if expires is not None and expires <= datetime.now(UTC):
            await self._terminate_queued(entry, CommandState.EXPIRED, "EXPIRED")
            return False
        return True

    async def _acquire_gate(self, entry: _Entry) -> bool:
        acquire = asyncio.create_task(self._gate.acquire())
        wakeup = asyncio.create_task(entry.wakeup.wait()) if entry.wakeup else None
        interrupted = False
        try:
            participants = {acquire}
            if wakeup is not None:
                participants.add(wakeup)
            await asyncio.wait(
                participants,
                timeout=max(entry.deadline - time.monotonic(), 0.0),
                return_when=asyncio.FIRST_COMPLETED,
            )
        except asyncio.CancelledError:
            interrupted = True
        finally:
            if wakeup is not None:
                wakeup.cancel()
                await asyncio.gather(wakeup, return_exceptions=True)
            if not acquire.done():
                acquire.cancel()
                await asyncio.gather(acquire, return_exceptions=True)
        acquired = (
            acquire.done()
            and not acquire.cancelled()
            and acquire.exception() is None
            and acquire.result()
        )
        if interrupted:
            if acquired:
                self._gate.release()
            raise asyncio.CancelledError
        if not acquired or entry.terminal_in_queue or entry.deadline <= time.monotonic():
            if acquired:
                self._gate.release()
            if not entry.terminal_in_queue:
                await self._terminate_queued(entry, CommandState.EXPIRED, "QUEUE_TIMEOUT")
            return False
        return True

    async def _execute(self, device_id: str, entry: _Entry) -> None:
        command_id = entry.command.command_id
        if not await self._check_before_write(entry, queued=True):
            return
        if device_id in self._blocked:
            await self._terminate_queued(entry, CommandState.FAILED, "DEVICE_BLOCKED")
            return

        # Only the FIFO head competes for a global connection/write permit.
        if not await self._acquire_gate(entry):
            return

        # A global permit ends queue waiting; subsequent connect is not queue time.
        entry.gate_acquired = True
        if entry.deadline_handle is not None:
            entry.deadline_handle.cancel()
        owns_gate = True
        try:
            if not await self._check_before_write(entry, queued=False):
                return
            session = self._sessions.get(device_id)
            if session is None:
                try:
                    session = self._factory(device_id)
                except Exception:
                    await self._unsent(command_id, CommandState.FAILED, "SESSION_FACTORY_FAILED")
                    return
                self._sessions[device_id] = session
            if not session.is_open():
                await self._control.connecting(command_id)
                connect = asyncio.create_task(session.connect())
                connected, owns_gate = await self._await_connect(
                    device_id, connect, self._connect_timeout, command_id
                )
                if not connected:
                    return
            if not await self._check_before_write(entry, queued=False):
                return
            group = self._batch_entries(device_id, entry, session)
            # Each member shares the acquired permit; no queued timeout may
            # race the in-memory T2 transitions.
            for member in group:
                member.gate_acquired = True
                if member.deadline_handle is not None:
                    member.deadline_handle.cancel()
            batch_completed = False
            try:
                # Validate and normalize every value before recording T2.
                writes = tuple(self._prepare_write(member.command.spec) for member in group)
                # Record the complete T2 batch in memory before native I/O.
                await self._control.executing_many(
                    tuple(member.command.command_id for member in group)
                )
                if any(
                    member.command.spec.expires_at is not None
                    and member.command.spec.expires_at <= datetime.now(UTC)
                    for member in group
                ):
                    # T2 recorded in memory; no native write invoked.
                    await self._control.complete_many(
                        tuple(
                            (
                                member.command.command_id,
                                CommandState.EXPIRED,
                                CommandOutcome.NOT_SENT,
                                "EXPIRED_BEFORE_WRITE",
                            )
                            for member in group
                        )
                    )
                    batch_completed = True
                    return
                progress: list[ProtocolWriteResult] = []
                stop_unstarted = asyncio.Event()
                started_count = [0]
                native = asyncio.create_task(
                    self._write_group(
                        session,
                        writes,
                        progress,
                        tuple(member.command for member in group),
                        stop_unstarted,
                        started_count,
                    )
                )
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(native), timeout=self._write_timeout
                    )
                except (TimeoutError, asyncio.CancelledError):
                    # Prevent any not-yet-started serial writes after the caller
                    # has observed a terminal result. The active write remains
                    # owned until native completion and physical isolation.
                    stop_unstarted.set()
                    self._block_late(device_id, native)
                    owns_gate = False
                    changes = self._serial_terminal_changes(
                        group,
                        tuple(progress),
                        "WRITE_TIMEOUT",
                        unsettled=True,
                        started_count=started_count[0],
                    )
                    self._track_unknown(device_id, changes)
                    await self._control.complete_many(changes)
                except _SerialWriteError as exc:
                    self._blocked.add(device_id)
                    self._retain_gate(device_id)
                    owns_gate = False
                    changes = self._serial_terminal_changes(
                        group,
                        exc.results,
                        type(exc.cause).__name__,
                        unsettled=False,
                        current_unconfirmed=exc.current_unconfirmed,
                    )
                    self._track_unknown(device_id, changes)
                    await self._control.complete_many(changes)
                    self._schedule_auto_isolation(device_id)
                except Exception as exc:
                    self._blocked.add(device_id)
                    self._retain_gate(device_id)
                    owns_gate = False
                    changes = self._serial_terminal_changes(
                        group, (), type(exc).__name__, unsettled=True
                    )
                    self._track_unknown(device_id, changes)
                    await self._control.complete_many(changes)
                    self._schedule_auto_isolation(device_id)
                else:
                    results = result if isinstance(result, tuple) else (result,)
                    if len(results) != len(group) or any(
                        not isinstance(actual, ProtocolWriteResult)
                        or actual.point_id != member.command.spec.point_id
                        for member, actual in zip(group, results, strict=False)
                    ):
                        self._blocked.add(device_id)
                        self._retain_gate(device_id)
                        owns_gate = False
                        changes = tuple(
                            (
                                member.command.command_id,
                                CommandState.UNKNOWN,
                                CommandOutcome.UNKNOWN,
                                "WRITE_RESULT_MISMATCH",
                            )
                            for member in group
                        )
                        self._track_unknown(device_id, changes)
                        await self._control.complete_many(changes)
                        self._schedule_auto_isolation(device_id)
                    else:
                        uncertain = any(
                            actual.outcome
                            not in (
                                WriteOutcome.CONFIRMED_SUCCESS,
                                WriteOutcome.CONFIRMED_FAILURE,
                            )
                            for actual in results
                        )
                        changes = tuple(
                            (
                                member.command.command_id,
                                (
                                    CommandState.SUCCEEDED
                                    if actual.outcome is WriteOutcome.CONFIRMED_SUCCESS
                                    else CommandState.FAILED
                                    if actual.outcome is WriteOutcome.CONFIRMED_FAILURE
                                    else CommandState.UNKNOWN
                                ),
                                (
                                    CommandOutcome.CONFIRMED_SUCCESS
                                    if actual.outcome is WriteOutcome.CONFIRMED_SUCCESS
                                    else CommandOutcome.CONFIRMED_FAILURE
                                    if actual.outcome is WriteOutcome.CONFIRMED_FAILURE
                                    else CommandOutcome.UNKNOWN
                                ),
                                (
                                    None
                                    if actual.outcome is WriteOutcome.CONFIRMED_SUCCESS
                                    else "DEVICE_REJECTED"
                                    if actual.outcome is WriteOutcome.CONFIRMED_FAILURE
                                    else "WRITE_UNKNOWN"
                                ),
                            )
                            for member, actual in zip(group, results, strict=True)
                        )
                        if uncertain:
                            self._blocked.add(device_id)
                        if uncertain:
                            self._track_unknown(device_id, changes)
                        await self._control.complete_many(changes)
                        if uncertain:
                            self._retain_gate(device_id)
                            owns_gate = False
                            self._schedule_auto_isolation(device_id)
                batch_completed = True
            finally:
                # Only signal completion after every in-memory terminal decision.
                if batch_completed:
                    for member in group[1:]:
                        if member in self._queues[device_id]:
                            self._queues[device_id].remove(member)
                        self._entries.pop(member.command.command_id, None)
                        if member.deadline_handle is not None:
                            member.deadline_handle.cancel()
                        if not member.finished.done():
                            member.finished.set_result(None)
        finally:
            if owns_gate:
                self._gate.release()

    @staticmethod
    def _serial_terminal_changes(
        group: tuple[_Entry, ...],
        confirmed: tuple[ProtocolWriteResult, ...],
        error_code: str,
        *,
        unsettled: bool,
        current_unconfirmed: bool = True,
        started_count: int | None = None,
    ) -> tuple[tuple[str, CommandState, CommandOutcome, str | None], ...]:
        changes: list[tuple[str, CommandState, CommandOutcome, str | None]] = []
        for index, member in enumerate(group):
            if index < len(confirmed):
                actual = confirmed[index]
                if actual.outcome is WriteOutcome.CONFIRMED_SUCCESS:
                    state, outcome, code = (
                        CommandState.SUCCEEDED,
                        CommandOutcome.CONFIRMED_SUCCESS,
                        None,
                    )
                elif actual.outcome is WriteOutcome.CONFIRMED_FAILURE:
                    state, outcome, code = (
                        CommandState.FAILED,
                        CommandOutcome.CONFIRMED_FAILURE,
                        "DEVICE_REJECTED",
                    )
                else:
                    state, outcome, code = (
                        CommandState.UNKNOWN,
                        CommandOutcome.UNKNOWN,
                        "WRITE_UNKNOWN",
                    )
            elif started_count is not None and index >= started_count:
                state, outcome, code = (
                    CommandState.INTERRUPTED,
                    CommandOutcome.NOT_SENT,
                    "BATCH_ABORTED_NOT_SENT",
                )
            elif unsettled or (current_unconfirmed and index == len(confirmed)):
                state, outcome, code = (CommandState.UNKNOWN, CommandOutcome.UNKNOWN, error_code)
            else:
                state, outcome, code = (
                    CommandState.INTERRUPTED,
                    CommandOutcome.NOT_SENT,
                    "BATCH_ABORTED_NOT_SENT",
                )
            changes.append((member.command.command_id, state, outcome, code))
        return tuple(changes)

    async def _write_group(
        self,
        session: WriteSession,
        writes: tuple[ProtocolWrite, ...],
        progress: list[ProtocolWriteResult],
        commands: tuple[AcceptedCommand, ...],
        stop_unstarted: asyncio.Event,
        started_count: list[int],
    ) -> tuple[ProtocolWriteResult, ...]:
        """Native batch for real devices; command-aware calls retain idempotent identity."""
        authorized = isinstance(session, AuthorizedWriteSession)
        if (
            not authorized
            and len(writes) > 1
            and ProtocolCapability.WRITE_MANY in session.capabilities()
        ):
            started_count[0] = len(writes)
            return await session.write_many(writes)
        for write, command in zip(writes, commands, strict=True):
            if stop_unstarted.is_set():
                break
            started_count[0] += 1
            try:
                if isinstance(session, AuthorizedWriteSession):
                    result = await session.write_authorized(command)
                else:
                    result = await session.write_one(write)
            except Exception as exc:
                raise _SerialWriteError(tuple(progress), exc, current_unconfirmed=True) from exc
            progress.append(result)
            if result.outcome is WriteOutcome.UNKNOWN:
                raise _SerialWriteError(
                    tuple(progress),
                    RuntimeError("unknown serial write"),
                    current_unconfirmed=False,
                )
        return tuple(progress)

    def _batch_entries(
        self, device_id: str, head: _Entry, session: WriteSession
    ) -> tuple[_Entry, ...]:
        if self._max_batch_points == 1:
            return (head,)
        group = [head]
        for member in list(self._queues[device_id])[1 : self._max_batch_points]:
            spec = member.command.spec
            if (
                member.terminal_in_queue
                or member.finished.done()
                or member.deadline <= time.monotonic()
                or member.command.request_id != head.command.request_id
                or member.deadline != head.deadline
                or spec.expires_at != head.command.spec.expires_at
                or spec.config_reference != head.command.spec.config_reference
                or (spec.expires_at is not None and spec.expires_at <= datetime.now(UTC))
                or not self._authorize(spec)
            ):
                break
            group.append(member)
        return tuple(group)

    async def _await_connect(
        self,
        device_id: str,
        task: asyncio.Task[None],
        timeout: float,
        command_id: str,
    ) -> tuple[bool, bool]:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except (TimeoutError, asyncio.CancelledError):
            self._pending_unknown[device_id].add(command_id)
            self._block_late(device_id, task)
            await self._unsent(command_id, CommandState.FAILED, "CONNECT_TIMEOUT")
            return False, False
        except Exception:
            await self._unsent(command_id, CommandState.FAILED, "CONNECT_FAILED")
            session = self._sessions[device_id]
            closing = asyncio.create_task(session.close())
            try:
                await asyncio.wait_for(asyncio.shield(closing), timeout=self._connect_timeout)
            except TimeoutError:
                self._pending_unknown[device_id].add(command_id)
                self._block_late(device_id, closing)
                return False, False
            except Exception:
                self._pending_unknown[device_id].add(command_id)
                self._blocked.add(device_id)
                self._retain_gate(device_id)
                return False, False
            self._sessions.pop(device_id, None)
            self._record_io_settled(command_id)
            return False, True
        return True, True

    async def _unknown(self, command_id: str, error_code: str) -> None:
        await self._control.complete(
            command_id,
            CommandState.UNKNOWN,
            outcome=CommandOutcome.UNKNOWN,
            error_code=error_code,
        )

    def _retain_gate(self, device_id: str) -> None:
        """Transfer the current permit into retained ownership until isolation."""
        self._retained_permits[device_id] += 1

    def _block_late(self, device_id: str, native: asyncio.Task[Any]) -> None:
        self._blocked.add(device_id)
        self._retain_gate(device_id)
        self._native.add(native)
        self._native_by_device[device_id].add(native)

        def finished(task: asyncio.Task[Any]) -> None:
            self._native.discard(task)
            self._native_by_device[device_id].discard(task)
            with contextlib.suppress(BaseException):
                task.result()
            # Completion alone is insufficient; verify safe physical close.
            if not self._native_by_device[device_id]:
                self._schedule_auto_isolation(device_id)

        native.add_done_callback(finished)

    def _schedule_auto_isolation(self, device_id: str) -> None:
        if (
            device_id not in self._blocked
            or self._native_by_device[device_id]
            or device_id in self._isolation_tasks
        ):
            return
        task = asyncio.create_task(self.isolate_device(device_id))
        self._auto_recovery.add(task)

        def settled(completed: asyncio.Task[bool]) -> None:
            self._auto_recovery.discard(completed)
            try:
                completed.result()
            except BaseException:
                self._blocked.add(device_id)

        task.add_done_callback(settled)

    async def isolate_device(self, device_id: str) -> bool:
        """Clear blocking only after native work settles and physical close succeeds."""
        if device_id not in self._blocked or self._native_by_device[device_id]:
            return False
        existing = self._isolation_tasks.get(device_id)
        if existing is None:
            session = self._sessions.get(device_id)
            if session is None:
                self._blocked.discard(device_id)
                self._release_retained(device_id)
                self._confirm_unknown_settled(device_id)
                return True
            existing = asyncio.create_task(session.close())
            self._isolation_tasks[device_id] = existing
        try:
            await asyncio.shield(existing)
        except asyncio.CancelledError:
            return False
        except Exception:
            self._isolation_tasks.pop(device_id, None)
            self._blocked.add(device_id)
            return False
        self._isolation_tasks.pop(device_id, None)
        self._sessions.pop(device_id, None)
        self._blocked.discard(device_id)
        self._release_retained(device_id)
        self._confirm_unknown_settled(device_id)
        return True

    def _release_retained(self, device_id: str) -> None:
        for _ in range(self._retained_permits.pop(device_id, 0)):
            self._gate.release()

    async def _expire_session(self, device_id: str) -> None:
        await asyncio.sleep(self._idle_timeout)
        if self._queues[device_id] or device_id in self._blocked:
            return
        session = self._sessions.get(device_id)
        if session is None:
            return
        self._idle_closing.add(device_id)
        try:
            await session.close()
            self._sessions.pop(device_id, None)
        except BaseException:
            self._blocked.add(device_id)
        finally:
            self._idle_closing.discard(device_id)

    async def close(self, *, grace_seconds: float = 10.0) -> frozenset[str]:
        """Bound shutdown; keep native tasks and sessions owned when unresolved."""
        if grace_seconds <= 0:
            raise ValueError("grace_seconds must be positive")
        self._closed = True
        try:
            await asyncio.wait_for(self._admission_lock.acquire(), timeout=grace_seconds)
        except TimeoutError:
            return frozenset({"__admission__"})
        else:
            self._admission_lock.release()
        for entry in tuple(self._entries.values()):
            if entry.deadline_handle is not None:
                entry.deadline_handle.cancel()
        # A hung FIFO head must never strand later accepted commands. Finalize
        # entries that have not acquired the I/O gate before waiting on workers;
        # the active native operation retains its original ownership.
        for entry in tuple(self._entries.values()):
            if not entry.gate_acquired and not entry.finished.done():
                await self._terminate_queued(
                    entry, CommandState.INTERRUPTED, "SERVICE_STOPPING"
                )
        if self._deadline_tasks:
            _, pending_deadlines = await asyncio.wait(
                tuple(self._deadline_tasks), timeout=grace_seconds
            )
            if pending_deadlines:
                return frozenset({"__deadlines__"})
        workers = tuple(self._workers.values())
        if workers:
            _, pending = await asyncio.wait(workers, timeout=grace_seconds)
            for task in pending:
                # Do not cancel: the task may own a still-active protocol write.
                for device_id, worker in self._workers.items():
                    if worker is task:
                        self._blocked.add(device_id)
        for device_id, idle in tuple(self._idle_tasks.items()):
            if device_id not in self._idle_closing:
                idle.cancel()
        if self._idle_tasks:
            await asyncio.wait(tuple(self._idle_tasks.values()), timeout=self._connect_timeout)
        for device_id, session in tuple(self._sessions.items()):
            active_worker: asyncio.Task[None] | None = self._workers.get(device_id)
            if device_id in self._blocked or (
                active_worker is not None and not active_worker.done()
            ):
                self._blocked.add(device_id)
                continue
            closing: asyncio.Task[None] | None = self._isolation_tasks.get(device_id)
            if closing is None:
                closing = asyncio.create_task(session.close())
                self._isolation_tasks[device_id] = closing
            try:
                await asyncio.wait_for(
                    asyncio.shield(closing),
                    timeout=self._connect_timeout,
                )
            except Exception:
                self._blocked.add(device_id)
            else:
                self._isolation_tasks.pop(device_id, None)
                self._sessions.pop(device_id, None)
        return self.blocked_devices()
