"""有界协议运行事件观察，独立于连接生命周期。"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from uuid import uuid4

from core.application.protocol_contract import (
    ProtocolLifecycleState,
    ProtocolOperation,
    ProtocolOperationEvent,
    ProtocolOperationState,
    ProtocolRuntimeState,
)

logger = logging.getLogger(__name__)
ProtocolEventCallback = Callable[[ProtocolOperationEvent], Awaitable[None]]


class ProtocolEventHandle:
    """观察者拥有的幂等注销句柄。"""

    def __init__(self, owner: ProtocolEventStream, key: int) -> None:
        self._owner: ProtocolEventStream | None = owner
        self._key = key

    async def close(self) -> None:
        owner = self._owner
        self._owner = None
        if owner is not None:
            await owner.remove(self._key)


class _Observer:
    def __init__(self, callback: ProtocolEventCallback, capacity: int) -> None:
        self.queue: asyncio.Queue[ProtocolOperationEvent] = asyncio.Queue(maxsize=capacity)
        self.callback = callback
        self.closed = False
        self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while not self.closed:
            event = await self.queue.get()
            try:
                if not self.closed:
                    await self.callback(event)
            except Exception:
                logger.exception("Protocol event observer failed")
            finally:
                self.queue.task_done()
            if self.closed:
                return


class ProtocolEventStream:
    """快照实时更新；事件是有界、允许丢失的观察通道，不是可靠审计。"""

    def __init__(
        self, lifecycle: ProtocolLifecycleState = ProtocolLifecycleState.DISCONNECTED
    ) -> None:
        self.driver_id = uuid4().hex
        self._lifecycle = lifecycle
        self._sequence = 0
        self._dropped = 0
        self._active: dict[str, ProtocolOperationEvent] = {}
        self._observers: dict[int, _Observer] = {}
        self._next_key = 0

    def snapshot(self, lifecycle: ProtocolLifecycleState | None = None) -> ProtocolRuntimeState:
        if lifecycle is not None:
            self._lifecycle = lifecycle
        return ProtocolRuntimeState(
            driver_id=self.driver_id,
            lifecycle=self._lifecycle,
            active_operations=tuple(self._active.values()),
            dropped_events=self._dropped,
        )

    def observe(self, callback: ProtocolEventCallback) -> ProtocolEventHandle:
        key = self._next_key
        self._next_key += 1
        self._observers[key] = _Observer(callback, capacity=256)
        return ProtocolEventHandle(self, key)

    async def remove(self, key: int) -> None:
        observer = self._observers.pop(key, None)
        if observer is None:
            return
        observer.closed = True
        if asyncio.current_task() is observer.task:
            # 自关闭由当前回调正常返回后退出循环，不能取消/等待自身。
            return
        observer.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await observer.task

    def transition(self, lifecycle: ProtocolLifecycleState) -> None:
        self._lifecycle = lifecycle

    def emit(
        self,
        operation_id: str,
        operation: ProtocolOperation,
        state: ProtocolOperationState,
        *,
        phase: str | None = None,
        point_id: str | None = None,
        message: str | None = None,
    ) -> None:
        self._sequence += 1
        event = ProtocolOperationEvent(
            driver_id=self.driver_id,
            sequence=self._sequence,
            operation_id=operation_id,
            operation=operation,
            state=state,
            timestamp=datetime.now(UTC),
            phase=phase,
            point_id=point_id,
            message=message,
        )
        if state in {ProtocolOperationState.RUNNING, ProtocolOperationState.QUEUED}:
            self._active[operation_id] = event
        else:
            self._active.pop(operation_id, None)
        for observer in self._observers.values():
            try:
                observer.queue.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped += 1
