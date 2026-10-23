"""跨 RPC 与仿真线程的命令接收及执行结果回传。"""

from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
from threading import Lock

from seahorse.domain.commands import EquipmentCommand


@dataclass(frozen=True, slots=True)
class PendingCommand:
    command: EquipmentCommand
    outcome: Future[None]


class CommandQueueFullError(RuntimeError):
    """命令队列背压拒绝。"""


class CommandQueueClosedError(RuntimeError):
    """命令队列已经关闭。"""


class CommandQueue:
    """去重记录贯穿进程生命期；超时命令不自动再次执行。"""

    def __init__(self, max_pending: int = 1024, max_command_ids: int = 1000000) -> None:
        if max_pending <= 0 or max_command_ids <= 0:
            raise ValueError("queue capacities must be positive")
        self._pending: deque[PendingCommand] = deque()
        self._ids: set[str] = set()
        self._max = max_pending
        self._max_command_ids = max_command_ids
        self._lock = Lock()
        self._closed = False

    def enqueue(self, command: EquipmentCommand) -> Future[None]:
        if not command.command_id:
            raise ValueError("command_id required")
        with self._lock:
            if self._closed:
                raise CommandQueueClosedError("command queue closed")
            if command.command_id in self._ids:
                raise ValueError("duplicate command_id")
            if len(self._ids) >= self._max_command_ids:
                raise CommandQueueFullError("command identity capacity exhausted; restart requires reconciliation")
            if len(self._pending) >= self._max:
                raise CommandQueueFullError("command queue full")
            result: Future[None] = Future()
            self._pending.append(PendingCommand(command, result))
            self._ids.add(command.command_id)
            return result

    def drain(self) -> tuple[PendingCommand, ...]:
        with self._lock:
            batch = tuple(self._pending)
            self._pending.clear()
            return batch

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for item in self._pending:
                item.outcome.set_exception(RuntimeError("runtime stopped before execution"))
            self._pending.clear()
