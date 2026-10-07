"""Shared Core Runtime 生命周期协调。"""

from __future__ import annotations

from collections.abc import Sequence

from core.application import CollectionAssignment

from .collection import CollectionRuntime
from .connection import ConnectionRuntime
from .sink import SinkRuntime


class CoreRuntime:
    """协调 Connection / Sink / Collection 三个 Runtime 子系统。

    启动顺序：Sink -> Connection -> Collection。
    停止顺序：Collection -> Connection -> Sink。
    子系统各自拥有自己的资源，本对象只负责跨子系统生命周期顺序。
    """

    def __init__(
        self,
        connections: ConnectionRuntime,
        sinks: SinkRuntime,
        collections: CollectionRuntime,
    ) -> None:
        self._connections = connections
        self._sinks = sinks
        self._collections = collections
        self._started = False

    @property
    def started(self) -> bool:
        """Runtime 是否已完成启动。"""
        return self._started

    async def start(self, assignments: Sequence[CollectionAssignment]) -> None:
        """启动全部 Runtime 资源与采集 assignment。"""
        if self._started:
            raise RuntimeError("core runtime is already started")

        sinks_started = False
        connections_started = False
        try:
            await self._sinks.start()
            sinks_started = True

            await self._connections.start()
            connections_started = True

            for assignment in assignments:
                await self._collections.start_assignment(assignment)
        except Exception:
            await self._rollback_start(
                connections_started=connections_started,
                sinks_started=sinks_started,
            )
            raise

        self._started = True

    async def stop(self) -> None:
        """按依赖反序停止全部 Runtime 子系统。"""
        if not self._started:
            return

        first_error: Exception | None = None

        try:
            await self._collections.stop()
        except Exception as exc:
            first_error = exc

        try:
            await self._connections.stop()
        except Exception as exc:
            if first_error is None:
                first_error = exc

        try:
            await self._sinks.stop()
        except Exception as exc:
            if first_error is None:
                first_error = exc

        self._started = False

        if first_error is not None:
            raise first_error

    async def _rollback_start(
        self,
        *,
        connections_started: bool,
        sinks_started: bool,
    ) -> None:
        """启动失败时按反序尽量回滚已建立资源。"""
        try:
            await self._collections.stop()
        except Exception:
            pass

        if connections_started:
            try:
                await self._connections.stop()
            except Exception:
                pass

        if sinks_started:
            try:
                await self._sinks.stop()
            except Exception:
                pass
