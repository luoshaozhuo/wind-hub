"""持续采集 Runtime。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime

from core.application import (
    AcquisitionMode,
    CollectionAssignment,
    PointValue,
    ProtocolSample,
    SubscribableProtocolPort,
    SubscriptionHandle,
    interpret_protocol_sample,
)
from core.config import ConfigSnapshot
from core.domain import DeviceId, ProtocolPoint, TaskId

from .connection import ConnectionRuntime
from .sink import SinkRuntime

_AssignmentKey = tuple[TaskId, DeviceId]


def _utc_now() -> datetime:
    """返回当前 UTC 时间。"""
    return datetime.now(UTC)


class CollectionRuntime:
    """CollectionAssignment 持续执行生命周期的唯一 owner。

    Runtime 不再解析 DeviceGroup、PointSet 或 Connection 候选；这些都必须在
    Application planning / placement 阶段收敛为 CollectionAssignment。
    """

    def __init__(
        self,
        snapshot: ConfigSnapshot,
        connections: ConnectionRuntime,
        sinks: SinkRuntime,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._snapshot = snapshot
        self._connections = connections
        self._sinks = sinks
        self._clock = clock
        self._poll_tasks: dict[_AssignmentKey, asyncio.Task[None]] = {}
        self._subscriptions: dict[_AssignmentKey, SubscriptionHandle] = {}

    async def start_assignment(self, assignment: CollectionAssignment) -> None:
        """启动一个已完成 placement 的采集 assignment。"""
        key = self._key(assignment)
        if key in self._poll_tasks or key in self._subscriptions:
            raise ValueError(
                f"collection assignment '{assignment.task_id}/{assignment.device_id}' "
                "is already running"
            )

        protocol = self._connections.protocol(assignment.connection_id)
        points = self._resolve_points(assignment)

        if protocol.acquisition_mode is AcquisitionMode.POLL:
            if assignment.interval is None or assignment.interval <= 0:
                raise ValueError(
                    f"poll assignment '{assignment.task_id}/{assignment.device_id}' "
                    "requires a positive interval"
                )
            self._poll_tasks[key] = asyncio.create_task(
                self._poll_loop(assignment, points)
            )
            return

        if protocol.acquisition_mode is not AcquisitionMode.SUBSCRIBE:
            raise ValueError(
                f"unsupported acquisition mode '{protocol.acquisition_mode}'"
            )
        if not isinstance(protocol, SubscribableProtocolPort):
            raise TypeError(
                f"connection '{assignment.connection_id}' declares subscribe mode "
                "but does not implement SubscribableProtocolPort"
            )

        async def _on_sample(sample: ProtocolSample) -> None:
            value = interpret_protocol_sample(
                self._snapshot,
                assignment.device_id,
                sample,
                observed_at=self._clock(),
            )
            await self._sinks.dispatch(assignment.target_sink_ids, (value,))

        handle = await protocol.subscribe(
            points,
            _on_sample,
            interval=assignment.interval,
        )
        self._subscriptions[key] = handle

    async def stop_assignment(self, task_id: TaskId, device_id: DeviceId) -> None:
        """停止一个采集 assignment；不存在时保持幂等。"""
        key = (task_id, device_id)

        task = self._poll_tasks.pop(key, None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        handle = self._subscriptions.pop(key, None)
        if handle is not None:
            await handle.close()

    async def stop(self) -> None:
        """停止全部采集实例。"""
        keys = tuple({*self._poll_tasks.keys(), *self._subscriptions.keys()})
        first_error: Exception | None = None
        for task_id, device_id in keys:
            try:
                await self.stop_assignment(task_id, device_id)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    async def _poll_loop(
        self,
        assignment: CollectionAssignment,
        points: tuple[ProtocolPoint, ...],
    ) -> None:
        assert assignment.interval is not None
        protocol = self._connections.protocol(assignment.connection_id)

        while True:
            samples = await protocol.read(points)
            values = self._interpret_batch(assignment, points, samples)
            await self._sinks.dispatch(assignment.target_sink_ids, values)
            await asyncio.sleep(assignment.interval)

    def _interpret_batch(
        self,
        assignment: CollectionAssignment,
        points: tuple[ProtocolPoint, ...],
        samples: tuple[ProtocolSample, ...],
    ) -> tuple[PointValue, ...]:
        expected_ids = {point.point_id for point in points}
        actual_ids = {sample.point_id for sample in samples}
        if actual_ids != expected_ids or len(samples) != len(points):
            raise ValueError(
                f"connection '{assignment.connection_id}' returned unexpected point set"
            )

        observed_at = self._clock()
        return tuple(
            interpret_protocol_sample(
                self._snapshot,
                assignment.device_id,
                sample,
                observed_at=observed_at,
            )
            for sample in samples
        )

    def _resolve_points(
        self,
        assignment: CollectionAssignment,
    ) -> tuple[ProtocolPoint, ...]:
        device = self._snapshot.devices[assignment.device_id]
        model = self._snapshot.device_models[device.device_model_id]
        table = self._snapshot.point_tables[model.point_table_id]
        return tuple(table.point(point_id) for point_id in assignment.point_ids)

    @staticmethod
    def _key(assignment: CollectionAssignment) -> _AssignmentKey:
        return assignment.task_id, assignment.device_id
