"""In-memory command state and bounded write-behind journal."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import defaultdict, deque
from collections.abc import Sequence
from concurrent.futures import Executor
from dataclasses import dataclass
from functools import partial
from typing import Any
from uuid import uuid4

from commander.application.ports.journal import JournalPort
from commander.application.metrics import CommandMetrics
from commander.domain.command import (
    TERMINAL_STATES,
    AcceptedCommand,
    CommandOutcome,
    CommandResult,
    CommandSpec,
    CommandState,
    request_fingerprint,
    utc_now,
    validate_transition,
)


@dataclass(frozen=True, slots=True)
class PendingEvent:
    fields: dict[str, Any]


class BufferedJournal:
    """Event-loop-owned volatile state with bounded pending durable events.

    Caller owns all mutations on the asyncio loop. Only snapshots are passed to
    the SQLite writer; the writer never accesses this mutable state.
    """

    def __init__(
        self,
        *,
        maximum_events: int = 100_000,
        initial_sequence: int = 0,
        metrics: CommandMetrics | None = None,
    ) -> None:
        if maximum_events <= 0 or initial_sequence < 0:
            raise ValueError("invalid buffer capacity or receive sequence")
        self._maximum = maximum_events
        self._metrics = metrics
        self._sequence = initial_sequence
        self._pending: deque[PendingEvent] = deque()
        self._pending_counts: dict[str, int] = defaultdict(int)
        self._evictable_requests: deque[tuple[str, str]] = deque()
        self._cache_limit = 10_000
        self._results: dict[str, CommandResult] = {}
        self._requests: dict[tuple[str, str], list[str]] = defaultdict(list)
        self._persisted = asyncio.Event()
        self._reserved: dict[str, int] = {}
        self._reserved_total = 0
        self._wake = asyncio.Event()
        self._closed = False

    @property
    def operational(self) -> bool:
        return not self._closed

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _capacity(self, amount: int) -> None:
        if self._closed:
            raise RuntimeError("persistence queue closed")
        if len(self._pending) + self._reserved_total + amount > self._maximum:
            raise OverflowError("persistence queue full")

    def _add(self, fields: dict[str, Any]) -> None:
        command_id = str(fields["command_id"])
        reserved = self._reserved.get(command_id, 0)
        if reserved:
            self._reserved[command_id] = reserved - 1
            self._reserved_total -= 1
        self._pending.append(PendingEvent(fields))
        self._pending_counts[command_id] += 1
        self._wake.set()

    def accept(
        self,
        specs: Sequence[CommandSpec],
        *,
        caller_id: str,
        request_id: str,
        operator_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[AcceptedCommand, ...]:
        if not specs or not caller_id.strip() or not request_id.strip():
            raise ValueError("nonempty batch, caller_id and request_id required")
        # Reserve T1, optional CONNECTING, T2 and T3 for every command.
        self._capacity(4 * len(specs))
        encoded = [json.dumps(spec.value, allow_nan=False) for spec in specs]
        fingerprint = request_fingerprint(specs)
        now = utc_now()
        accepted: list[AcceptedCommand] = []
        for index, (spec, value) in enumerate(zip(specs, encoded, strict=True)):
            self._sequence += 1
            command = AcceptedCommand(
                str(uuid4()), request_id, caller_id, spec, now, self._sequence
            )
            self._reserved[command.command_id] = 4
            self._reserved_total += 4
            self._results[command.command_id] = CommandResult(
                command, CommandState.ACCEPTED, None, None, None, None, now
            )
            self._requests[(caller_id, request_id)].append(command.command_id)
            self._add(
                {
                    "event_id": str(uuid4()),
                    "command_id": command.command_id,
                    "event_type": "ACCEPTED",
                    "occurred_at": now.isoformat(),
                    "request_id": request_id,
                    "caller_id": caller_id,
                    "operator_id": operator_id,
                    "trace_id": trace_id,
                    "device_id": spec.device_id,
                    "point_id": spec.point_id,
                    "requested_value": value,
                    "config_reference": spec.config_reference,
                    "receive_seq": self._sequence,
                    "state": CommandState.ACCEPTED.value,
                    "detail_json": json.dumps(
                        {
                            "expires_at": spec.expires_at.isoformat() if spec.expires_at else None,
                            "request_fingerprint": fingerprint,
                            "request_index": index,
                        }
                    ),
                }
            )
            accepted.append(command)
        if self._metrics is not None:
            self._metrics.accepted(len(accepted))
        return tuple(accepted)

    def transition(
        self,
        command_id: str,
        state: CommandState,
        *,
        outcome: CommandOutcome | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.transition_many(((command_id, state, outcome, error_code, error_message),))

    def transition_many(
        self,
        changes: Sequence[
            tuple[str, CommandState, CommandOutcome | None, str | None]
            | tuple[str, CommandState, CommandOutcome | None, str | None, str | None]
        ],
    ) -> None:
        if not changes:
            raise ValueError("empty transition batch")
        # Every accepted command owns reserved T2/T3 capacity.
        if len({change[0] for change in changes}) != len(changes):
            raise ValueError("duplicate transition")
        for change in changes:
            current = self._results[change[0]]
            validate_transition(current.state, change[1], change[2])
        now = utc_now()
        for change in changes:
            command_id = change[0]
            state = change[1]
            outcome = change[2]
            error_code = change[3]
            message = change[4] if len(change) == 5 else None
            previous = self._results[command_id]
            self._results[command_id] = CommandResult(
                previous.command,
                state,
                outcome,
                error_code,
                message,
                now if state in TERMINAL_STATES else None,
                now,
            )
            self._add(
                {
                    "event_id": str(uuid4()),
                    "command_id": command_id,
                    "event_type": "COMPLETED" if state in TERMINAL_STATES else state.value,
                    "occurred_at": now.isoformat(),
                    "state": state.value,
                    "outcome": outcome.value if outcome else None,
                    "error_code": error_code,
                    "error_message": message,
                }
            )
            if state in TERMINAL_STATES:
                if self._metrics is not None and outcome is not None:
                    self._metrics.completed(outcome.value)
                self._reserved_total -= self._reserved.pop(command_id, 0)

    def get_cached(self, command_id: str) -> CommandResult | None:
        return self._results.get(command_id)

    def get_request_cached(
        self, request_id: str, caller_id: str
    ) -> tuple[CommandResult, ...] | None:
        ids = self._requests.get((caller_id, request_id))
        if ids is None or any(item not in self._results for item in ids):
            return None
        return tuple(self._results[item] for item in ids)

    async def wait_persisted(self, command_ids: Sequence[str]) -> None:
        """Wait until all accepted T1 events have committed to SQLite."""
        while any(self._pending_counts.get(command_id, 0) for command_id in command_ids):
            self._persisted.clear()
            await self._persisted.wait()

    def snapshot(self, limit: int) -> tuple[dict[str, Any], ...]:
        """Keep every T1 item of a submitted request in one SQLite transaction."""
        events = list(self._pending)
        end = min(limit, len(events))
        while 0 < end < len(events):
            current = events[end - 1].fields
            following = events[end].fields
            if (
                current.get("event_type") != "ACCEPTED"
                or following.get("event_type") != "ACCEPTED"
                or current.get("request_id") != following.get("request_id")
                or current.get("caller_id") != following.get("caller_id")
            ):
                break
            end += 1
        return tuple(event.fields for event in events[:end])

    def confirm(self, count: int) -> None:
        affected: set[tuple[str, str]] = set()
        for _ in range(count):
            command_id = str(self._pending.popleft().fields["command_id"])
            self._pending_counts[command_id] -= 1
            if self._pending_counts[command_id] == 0:
                del self._pending_counts[command_id]
                command = self._results.get(command_id)
                if command is not None:
                    affected.add((command.command.caller_id, command.command.request_id))
        self._persisted.set()
        for request_key in affected:
            commands = self._requests.get(request_key, [])
            if commands and all(
                command_id not in self._pending_counts
                and self._results[command_id].state in TERMINAL_STATES
                for command_id in commands
            ):
                self._evictable_requests.append(request_key)
        while len(self._results) > self._cache_limit and self._evictable_requests:
            request_key = self._evictable_requests.popleft()
            for command_id in self._requests.pop(request_key, []):
                self._results.pop(command_id, None)
                self._reserved.pop(command_id, None)

    @property
    def wake(self) -> asyncio.Event:
        return self._wake

    def close(self) -> None:
        self._closed = True
        self._wake.set()


class PersistenceWorker:
    """Periodically flush immutable event batches via one SQLite writer."""

    def __init__(
        self,
        buffer: BufferedJournal,
        repository: JournalPort,
        writer: Executor,
        *,
        interval: float = 0.05,
        batch_size: int = 500,
    ) -> None:
        if interval <= 0 or batch_size <= 0:
            raise ValueError("invalid persistence worker interval or batch size")
        self._buffer = buffer
        self._repository = repository
        self._writer = writer
        self._interval = interval
        self._batch_size = batch_size
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._stop_event = asyncio.Event()
        self._failed: Exception | None = None

    @property
    def failure(self) -> Exception | None:
        return self._failed

    @property
    def operational(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="commander:persistence")

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stopping or self._buffer.pending_count:
            if not self._buffer.pending_count:
                self._buffer.wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._buffer.wake.wait(), timeout=self._interval)
                continue
            if not self._stopping and self._buffer.pending_count < self._batch_size:
                # Coalesce independent command events before one SQLite COMMIT.
                # 固定合并窗口（新事件不重置计时，避免持续细流饿死落盘）；
                # stop() 置位停机事件可立即中断窗口，无需等完整个 interval。
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
            snapshot = self._buffer.snapshot(self._batch_size)
            try:
                await loop.run_in_executor(
                    self._writer, partial(self._repository.append_events, snapshot)
                )
            except Exception as exc:
                self._failed = exc
                await asyncio.sleep(self._interval)
                continue
            self._failed = None
            self._buffer.confirm(len(snapshot))

    async def stop(self, timeout: float) -> bool:
        self._stopping = True
        self._stop_event.set()
        self._buffer.wake.set()
        if self._task is None:
            return True
        if self._task.done():
            # 任务已异常退出：等待它只会抛 CancelledError；停机路径必须
            # 如实返回 False，由服务映射 FAILED + __persistence_queue__。
            if not self._task.cancelled():
                exception = self._task.exception()
                if isinstance(exception, Exception):
                    self._failed = exception
            return False
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)
        except TimeoutError:
            return False
        except Exception as exc:
            self._failed = exc
            return False
        return True
