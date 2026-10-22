"""线程安全的待执行命令队列，仿真线程独占模型状态。"""
from __future__ import annotations

from collections import deque
from threading import Lock

from seahorse.domain.commands import EquipmentCommand


class CommandQueue:
    """接收仅表示入队成功；执行结果通过仿真测量反馈观察。"""

    def __init__(self, max_pending: int = 1024) -> None:
        if max_pending <= 0:
            raise ValueError("max_pending must be positive")
        self._pending: deque[EquipmentCommand] = deque()
        self._ids: set[str] = set()
        self._max = max_pending
        self._lock = Lock()

    def enqueue(self, command: EquipmentCommand) -> None:
        if not command.command_id:
            raise ValueError("command_id required")
        with self._lock:
            if command.command_id in self._ids:
                raise ValueError("duplicate command_id")
            if len(self._pending) >= self._max:
                raise RuntimeError("command queue full")
            self._pending.append(command)
            self._ids.add(command.command_id)

    def drain(self) -> tuple[EquipmentCommand, ...]:
        with self._lock:
            batch = tuple(self._pending)
            self._pending.clear()
            return batch
