"""Durable per-device FIFO execution with bounded global native I/O ownership."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from core.application.protocol_contract import ProtocolWrite, WriteOutcome

from commander.application.control import CommandControl
from commander.domain.command import (
    AcceptedCommand,
    CommandOutcome,
    CommandSpec,
    CommandState,
)


class WriteSession(Protocol):
    async def connect(self) -> None: ...
    async def write_one(self, write: ProtocolWrite) -> object: ...
    async def close(self) -> None: ...


SessionFactory = Callable[[str], Awaitable[WriteSession]]


@dataclass(slots=True)
class _Queued:
    command: AcceptedCommand
    deadline: float
    future: asyncio.Future[None]


class QueuedExecutor:
    """A device worker serializes native writes; global capacity includes lingering I/O.

    Only a successful native I/O settlement releases its ownership. Cancelling a
    client waiter cannot abandon native ownership. An unresolvable driver keeps
    its capacity and device blocked until service isolation.
    """

    def __init__(
        self,
        control: CommandControl,
        session_factory: SessionFactory,
        *,
        global_capacity: int = 32,
        queue_timeout: float = 5.0,
        connect_timeout: float = 3.0,
        write_timeout: float = 3.0,
        max_queued_per_device: int = 100,
    ) -> None:
        if global_capacity < 1 or max_queued_per_device < 1:
            raise ValueError("capacity must be positive")
        if min(queue_timeout, connect_timeout, write_timeout) <= 0:
            raise ValueError("timeouts must be positive")
        self._control = control
        self._factory = session_factory
        self._global = asyncio.Semaphore(global_capacity)
        self._queue_timeout = queue_timeout
        self._connect_timeout = connect_timeout
        self._write_timeout = write_timeout
        self._queue_limit = max_queued_per_device
        self._queues: dict[str, deque[_Queued]] = defaultdict(deque)
        self._workers: dict[str, asyncio.Task[None]] = {}
        self._sessions: dict[str, WriteSession] = {}
        self._blocked: set[str] = set()
        self._closed = False

    def accept(
        self,
        commands: Sequence[CommandSpec],
        *,
        caller_id: str,
    ) -> tuple[AcceptedCommand, ...]:
        """Durably accept before dispatch; capacity rejection occurs before acceptance."""
        if self._closed:
            raise RuntimeError("executor is closed")
        counts: dict[str, int] = defaultdict(int)
        for spec in commands:
            counts[spec.device_id] += 1
        if any(
            len(self._queues[device_id]) + count > self._queue_limit
            for device_id, count in counts.items()
        ):
            raise OverflowError("device command queue capacity exhausted")
        accepted = self._control.accept(commands, caller_id=caller_id)
        loop = asyncio.get_running_loop()
        for command in accepted:
            item = _Queued(
                command=command,
                deadline=time.monotonic() + self._queue_timeout,
                future=loop.create_future(),
            )
            self._queues[command.spec.device_id].append(item)
            self._ensure_worker(command.spec.device_id)
        return accepted

    def _ensure_worker(self, device_id: str) -> None:
        task = self._workers.get(device_id)
        if task is None or task.done():
            self._workers[device_id] = asyncio.create_task(
                self._run_device(device_id), name=f"commander:{device_id}"
            )

    async def _run_device(self, device_id: str) -> None:
        queue = self._queues[device_id]
        while queue:
            item = queue[0]
            try:
                await self._execute(device_id, item)
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    raise
                if not item.future.done():
                    item.future.set_exception(exc)
            finally:
                queue.popleft()

    async def _execute(self, device_id: str, item: _Queued) -> None:
        spec = item.command.spec
        command_id = item.command.command_id
        remaining = item.deadline - time.monotonic()
        if remaining <= 0:
            self._complete_unsent(command_id, CommandState.EXPIRED, "QUEUE_TIMEOUT")
            item.future.set_result(None)
            return
        if spec.expires_at is not None:
            from datetime import datetime, UTC

            if spec.expires_at <= datetime.now(UTC):
                self._complete_unsent(command_id, CommandState.EXPIRED, "EXPIRED")
                item.future.set_result(None)
                return
        if device_id in self._blocked:
            self._complete_unsent(command_id, CommandState.FAILED, "DEVICE_BLOCKED")
            item.future.set_result(None)
            return
        try:
            await asyncio.wait_for(self._global.acquire(), timeout=remaining)
        except TimeoutError:
            self._complete_unsent(command_id, CommandState.EXPIRED, "QUEUE_TIMEOUT")
            item.future.set_result(None)
            return
        owned = True
        try:
            session = self._sessions.get(device_id)
            if session is None:
                session = await self._factory(device_id)
                self._sessions[device_id] = session
                self._control.connecting(command_id)
                await asyncio.wait_for(session.connect(), timeout=self._connect_timeout)
            self._control.executing(command_id)
            native = asyncio.create_task(
                session.write_one(ProtocolWrite(point=spec.point_id, value=spec.value))
            )
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(native), timeout=self._write_timeout
                )
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
                    self._control.complete(
                        command_id,
                        CommandState.UNKNOWN,
                        outcome=CommandOutcome.UNKNOWN,
                        error_code="WRITE_UNKNOWN",
                    )
            except (TimeoutError, asyncio.CancelledError):
                self._control.complete(
                    command_id,
                    CommandState.UNKNOWN,
                    outcome=CommandOutcome.UNKNOWN,
                    error_code="WRITE_UNCERTAIN",
                )
                # A timed out driver still owns its native operation and capacity.
                owned = False
                self._blocked.add(device_id)
                native.add_done_callback(
                    lambda task: self._settle_late(device_id, task)
                )
            item.future.set_result(None)
        except Exception as exc:
            state = self._control.get_command(command_id)
            if state is not None and state.state == CommandState.EXECUTING:
                self._control.complete(
                    command_id,
                    CommandState.UNKNOWN,
                    outcome=CommandOutcome.UNKNOWN,
                    error_code="WRITE_EXCEPTION",
                    error_message=type(exc).__name__,
                )
                self._blocked.add(device_id)
            else:
                self._control.complete(
                    command_id,
                    CommandState.FAILED,
                    outcome=CommandOutcome.CONFIRMED_FAILURE,
                    error_code="CONNECT_FAILED",
                )
            item.future.set_result(None)
        finally:
            if owned:
                self._global.release()

    def _settle_late(self, device_id: str, task: asyncio.Task[object]) -> None:
        try:
            task.result()
        except BaseException:
            pass
        # A late completion does not itself prove the transport is reusable.
        # Retain device isolation; allow capacity for other devices.
        self._global.release()

    def _complete_unsent(
        self, command_id: str, state: CommandState, code: str
    ) -> None:
        self._control.complete(
            command_id,
            state,
            outcome=CommandOutcome.NOT_SENT
            if state in (CommandState.EXPIRED, CommandState.CANCELLED)
            else CommandOutcome.CONFIRMED_FAILURE,
            error_code=code,
        )

    async def await_command(self, command_id: str) -> None:
        for queue in self._queues.values():
            for item in queue:
                if item.command.command_id == command_id:
                    await asyncio.shield(item.future)
                    return

    async def close(self) -> None:
        self._closed = True
        await asyncio.gather(*self._workers.values(), return_exceptions=True)
        await asyncio.gather(
            *(session.close() for session in self._sessions.values()),
            return_exceptions=True,
        )
