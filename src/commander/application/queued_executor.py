"""Per-device FIFO dispatcher with durable decisions and safe native I/O ownership."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from core.application.protocol_contract import (
    ProtocolCapability,
    ProtocolWrite,
    ProtocolWriteResult,
    WriteOutcome,
)

from commander.application.control import CommandControl
from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
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


SessionFactory = Callable[[str], WriteSession]


@dataclass(slots=True)
class _Entry:
    command: AcceptedCommand
    deadline: float
    finished: asyncio.Future[None]
    cancelled: bool = False
    terminal_in_queue: bool = False
    wakeup: asyncio.Event | None = None
    deadline_handle: asyncio.TimerHandle | None = None


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
        *,
        global_capacity: int = 32,
        queue_timeout: float = 5.0,
        connect_timeout: float = 3.0,
        write_timeout: float = 3.0,
        max_queued_batches_per_device: int = 10,
        max_queued_batches_global: int = 1000,
        idle_timeout: float = 30.0,
        max_batch_points: int = 10,
    ) -> None:
        if (
            global_capacity < 1
            or max_queued_batches_per_device < 1
            or max_queued_batches_global < 1
            or max_batch_points < 1
        ):
            raise ValueError("capacities must be positive")
        if min(queue_timeout, connect_timeout, write_timeout, idle_timeout) <= 0:
            raise ValueError("timeouts must be positive")
        self._control = control
        self._factory = session_factory
        self._authorize = authorize
        self._gate = asyncio.Semaphore(global_capacity)
        self._queue_timeout = queue_timeout
        self._connect_timeout = connect_timeout
        self._write_timeout = write_timeout
        self._device_batch_limit = max_queued_batches_per_device
        self._global_batch_limit = max_queued_batches_global
        self._idle_timeout = idle_timeout
        self._max_batch_points = max_batch_points
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}
        self._idle_closing: set[str] = set()
        self._queues: dict[str, deque[_Entry]] = defaultdict(deque)
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._entries: dict[str, _Entry] = {}
        self._sessions: dict[str, WriteSession] = {}
        self._blocked: set[str] = set()
        self._native: set[asyncio.Task[Any]] = set()
        self._native_by_device: dict[str, set[asyncio.Task[object]]] = defaultdict(set)
        self._isolation_tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False
        self._fatal = False

    def accept(
        self,
        commands: Sequence[CommandSpec],
        *,
        caller_id: str,
    ) -> tuple[AcceptedCommand, ...]:
        """Atomically persist one batch before adding any command to FIFO."""
        if self._closed or self._fatal:
            raise RuntimeError("dispatcher unavailable")
        if len(commands) > self._max_batch_points:
            raise OverflowError("batch point limit exceeded")
        counts: dict[str, int] = defaultdict(int)
        for spec in commands:
            if not self._authorize(spec):
                raise PermissionError(
                    "command device/point/config reference is not authorized"
                )
            counts[spec.device_id] += 1
        for device_id in counts:
            pending = {item.command.request_id for item in self._queues[device_id]}
            if len(pending) >= self._device_batch_limit:
                raise OverflowError("per-device pending batch capacity exceeded")
        pending_global = {
            item.command.request_id
            for queue in self._queues.values()
            for item in queue
        }
        if len(pending_global) >= self._global_batch_limit:
            raise OverflowError("global pending batch capacity exceeded")
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        accepted = self._control.accept(commands, caller_id=caller_id)
        for command in accepted:
            entry = _Entry(
                command=command,
                deadline=started + self._queue_timeout,
                finished=loop.create_future(),
                wakeup=asyncio.Event(),
            )
            entry.deadline_handle = loop.call_at(
                entry.deadline, self._expire_queued, entry
            )
            self._entries[command.command_id] = entry
            self._queues[command.spec.device_id].append(entry)
        for device_id in counts:
            self._ensure_worker(device_id)
        return accepted

    def _terminate_queued(
        self, entry: _Entry, state: CommandState, code: str
    ) -> None:
        if entry.finished.done() or entry.terminal_in_queue:
            return
        try:
            self._unsent(entry.command.command_id, state, code)
        except Exception:
            self._fatal = True
            raise
        entry.terminal_in_queue = True
        if entry.wakeup is not None:
            entry.wakeup.set()
        queue = self._queues[entry.command.spec.device_id]
        if queue and queue[0] is not entry:
            queue.remove(entry)
            if entry.deadline_handle is not None:
                entry.deadline_handle.cancel()
            self._entries.pop(entry.command.command_id, None)
            entry.finished.set_result(None)

    def _expire_queued(self, entry: _Entry) -> None:
        if entry.finished.done() or entry.terminal_in_queue:
            return
        try:
            state = self._control.get_command(entry.command.command_id)
            if state is None or state.state is not CommandState.ACCEPTED:
                return
            self._terminate_queued(entry, CommandState.EXPIRED, "QUEUE_TIMEOUT")
        except Exception:
            self._fatal = True

    def cancel(self, command_id: str) -> bool:
        """Cancel only before the native write, without cancelling native I/O."""
        entry = self._entries.get(command_id)
        if entry is None or entry.finished.done() or entry.terminal_in_queue:
            return False
        state = self._control.get_command(command_id)
        if state is None:
            return False
        if state.state is CommandState.ACCEPTED:
            self._terminate_queued(entry, CommandState.CANCELLED, "CANCELLED")
            return True
        if state.state is CommandState.CONNECTING:
            entry.cancelled = True
            return True
        return False

    async def wait(self, command_id: str) -> CommandResult | None:
        entry = self._entries.get(command_id)
        if entry is not None:
            await asyncio.shield(entry.finished)
        return self._control.get_command(command_id)

    def blocked_devices(self) -> frozenset[str]:
        return frozenset(self._blocked)

    def _ensure_worker(self, device_id: str) -> None:
        task = self._workers.get(device_id)
        if task is None or task.done():
            self._workers[device_id] = asyncio.create_task(
                self._run_device(device_id), name=f"commander:{device_id}"
            )

    async def _run_device(self, device_id: str) -> None:
        idle = self._idle_tasks.pop(device_id, None)
        if idle is not None:
            if device_id not in self._idle_closing:
                idle.cancel()
            await asyncio.gather(idle, return_exceptions=True)
        queue = self._queues[device_id]
        while queue:
            entry = queue[0]
            try:
                await self._execute(device_id, entry)
            except Exception as exc:
                # A failed durable decision must halt subsequent field writes.
                self._fatal = True
                if not entry.finished.done():
                    entry.finished.set_exception(exc)
                return
            else:
                if not entry.finished.done():
                    entry.finished.set_result(None)
            finally:
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

    def _unsent(self, command_id: str, state: CommandState, code: str) -> None:
        self._control.complete(
            command_id,
            state,
            outcome=(
                CommandOutcome.CONFIRMED_FAILURE
                if state is CommandState.FAILED
                else CommandOutcome.NOT_SENT
            ),
            error_code=code,
        )

    def _check_before_write(self, entry: _Entry, *, queued: bool) -> bool:
        command_id = entry.command.command_id
        if self._fatal or entry.terminal_in_queue:
            return False
        if entry.cancelled:
            self._unsent(command_id, CommandState.CANCELLED, "CANCELLED")
            return False
        if queued and entry.deadline <= time.monotonic():
            self._unsent(command_id, CommandState.EXPIRED, "QUEUE_TIMEOUT")
            return False
        if not self._authorize(entry.command.spec):
            self._unsent(command_id, CommandState.FAILED, "CONFIG_CHANGED_OR_REVOKED")
            return False
        expires = entry.command.spec.expires_at
        if expires is not None and expires <= datetime.now(UTC):
            self._unsent(command_id, CommandState.EXPIRED, "EXPIRED")
            return False
        return True

    async def _acquire_gate(self, entry: _Entry) -> bool:
        acquire = asyncio.create_task(self._gate.acquire())
        wakeup = asyncio.create_task(entry.wakeup.wait()) if entry.wakeup else None
        try:
            participants = {acquire}
            if wakeup is not None:
                participants.add(wakeup)
            await asyncio.wait(
                participants,
                timeout=max(entry.deadline - time.monotonic(), 0.0),
                return_when=asyncio.FIRST_COMPLETED,
            )
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
        if (
            not acquired
            or entry.terminal_in_queue
            or entry.deadline <= time.monotonic()
        ):
            if acquired:
                self._gate.release()
            if not entry.terminal_in_queue:
                self._terminate_queued(entry, CommandState.EXPIRED, "QUEUE_TIMEOUT")
            return False
        return True

    async def _execute(self, device_id: str, entry: _Entry) -> None:
        command_id = entry.command.command_id
        if not self._check_before_write(entry, queued=True):
            return
        if device_id in self._blocked:
            self._unsent(command_id, CommandState.FAILED, "DEVICE_BLOCKED")
            return

        # Only the FIFO head competes for a global connection/write permit.
        if not await self._acquire_gate(entry):
            return

        owns_gate = True
        try:
            if not self._check_before_write(entry, queued=True):
                return
            session = self._sessions.get(device_id)
            if session is None:
                try:
                    session = self._factory(device_id)
                except Exception:
                    self._unsent(
                        command_id, CommandState.FAILED, "SESSION_FACTORY_FAILED"
                    )
                    return
                self._sessions[device_id] = session
            if not session.is_open():
                self._control.connecting(command_id)
                connect = asyncio.create_task(session.connect())
                connected, owns_gate = await self._await_connect(
                    device_id, connect, self._connect_timeout, command_id
                )
                if not connected:
                    return
            if not self._check_before_write(entry, queued=False):
                return
            group = self._batch_entries(device_id, entry, session)
            # Persist every member before any potentially effective field write.
            for member in group:
                self._control.executing(member.command.command_id)
            writes = tuple(
                ProtocolWrite(
                    point=member.command.spec.point_id,
                    value=member.command.spec.value,
                )
                for member in group
            )
            native = asyncio.create_task(
                session.write_many(writes)
                if len(group) > 1
                else session.write_one(writes[0])
            )
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(native), timeout=self._write_timeout
                )
            except (TimeoutError, asyncio.CancelledError):
                self._block_late(device_id, native)
                owns_gate = False
                for member in group:
                    self._unknown(member.command.command_id, "WRITE_TIMEOUT")
            except Exception as exc:
                self._blocked.add(device_id)
                for member in group:
                    self._unknown(member.command.command_id, type(exc).__name__)
            else:
                results = result if isinstance(result, tuple) else (result,)
                if (
                    len(results) != len(group)
                    or any(
                        not isinstance(actual, ProtocolWriteResult)
                        or actual.point_id != member.command.spec.point_id
                        for member, actual in zip(group, results, strict=False)
                    )
                ):
                    self._blocked.add(device_id)
                    for member in group:
                        self._unknown(
                            member.command.command_id, "WRITE_RESULT_MISMATCH"
                        )
                else:
                    for member, actual in zip(group, results, strict=True):
                        self._finish_write(
                            member.command.command_id, device_id, actual
                        )
            finally:
                # The head belongs to _run_device; remove any coalesced successors.
                for member in group[1:]:
                    self._queues[device_id].remove(member)
                    self._entries.pop(member.command.command_id, None)
                    if member.deadline_handle is not None:
                        member.deadline_handle.cancel()
                    if not member.finished.done():
                        member.finished.set_result(None)
        finally:
            if owns_gate:
                self._gate.release()

    def _batch_entries(
        self, device_id: str, head: _Entry, session: WriteSession
    ) -> tuple[_Entry, ...]:
        if (
            self._max_batch_points == 1
            or ProtocolCapability.WRITE_MANY not in session.capabilities()
        ):
            return (head,)
        group = [head]
        for member in list(self._queues[device_id])[1:self._max_batch_points]:
            spec = member.command.spec
            if (
                member.cancelled
                or member.deadline <= time.monotonic()
                or member.command.request_id != head.command.request_id
                or member.deadline != head.deadline
                or spec.expires_at != head.command.spec.expires_at
                or spec.config_reference != head.command.spec.config_reference
                or (
                    spec.expires_at is not None
                    and spec.expires_at <= datetime.now(UTC)
                )
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
            self._block_late(device_id, task)
            self._unsent(command_id, CommandState.FAILED, "CONNECT_TIMEOUT")
            return False, False
        except Exception:
            self._unsent(command_id, CommandState.FAILED, "CONNECT_FAILED")
            session = self._sessions[device_id]
            closing = asyncio.create_task(session.close())
            try:
                await asyncio.wait_for(
                    asyncio.shield(closing), timeout=self._connect_timeout
                )
            except TimeoutError:
                self._block_late(device_id, closing)
                return False, False
            except Exception:
                self._blocked.add(device_id)
                return False, True
            self._sessions.pop(device_id, None)
            return False, True
        return True, True

    def _finish_write(
        self,
        command_id: str,
        device_id: str,
        result: ProtocolWriteResult,
    ) -> None:
        if result.outcome is WriteOutcome.CONFIRMED_SUCCESS:
            self._control.complete(
                command_id,
                CommandState.SUCCEEDED,
                outcome=CommandOutcome.CONFIRMED_SUCCESS,
            )
        elif result.outcome is WriteOutcome.CONFIRMED_FAILURE:
            self._control.complete(
                command_id,
                CommandState.FAILED,
                outcome=CommandOutcome.CONFIRMED_FAILURE,
                error_code="DEVICE_REJECTED",
            )
        else:
            self._blocked.add(device_id)
            self._unknown(command_id, "WRITE_UNKNOWN")

    def _unknown(self, command_id: str, error_code: str) -> None:
        self._control.complete(
            command_id,
            CommandState.UNKNOWN,
            outcome=CommandOutcome.UNKNOWN,
            error_code=error_code,
        )

    def _block_late(
        self, device_id: str, native: asyncio.Task[object]
    ) -> None:
        self._blocked.add(device_id)
        self._native.add(native)
        self._native_by_device[device_id].add(native)

        def finished(task: asyncio.Task[object]) -> None:
            self._native.discard(task)
            self._native_by_device[device_id].discard(task)
            try:
                task.result()
            except BaseException:
                pass
            # Completion releases global capacity, but not device isolation.
            self._gate.release()

        native.add_done_callback(finished)

    async def isolate_device(self, device_id: str) -> bool:
        """Clear blocking only after native work settles and physical close succeeds."""
        if device_id not in self._blocked or self._native_by_device[device_id]:
            return False
        existing = self._isolation_tasks.get(device_id)
        if existing is None:
            session = self._sessions.get(device_id)
            if session is None:
                self._blocked.discard(device_id)
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
        return True

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

    async def close(self) -> None:
        """Stop admission, let active FIFO drain; never abandon native tasks."""
        self._closed = True
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        for device_id, idle in tuple(self._idle_tasks.items()):
            if device_id not in self._idle_closing:
                idle.cancel()
        await asyncio.gather(*self._idle_tasks.values(), return_exceptions=True)
        for device_id, session in tuple(self._sessions.items()):
            if device_id in self._blocked:
                continue
            try:
                await session.close()
            except Exception:
                self._blocked.add(device_id)
            else:
                self._sessions.pop(device_id, None)
