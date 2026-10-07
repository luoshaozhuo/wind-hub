"""持续采集 Runtime。"""

from __future__ import annotations

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
from .scheduler import FixedRateHandle
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
        on_poll_error: Callable[[CollectionAssignment, BaseException], None] | None = None,
        on_poll_stats: (
            Callable[[CollectionAssignment, float, bool, int], None] | None
        ) = None,
    ) -> None:
        self._snapshot = snapshot
        self._connections = connections
        self._sinks = sinks
        self._clock = clock
        self._on_poll_error = on_poll_error
        self._on_poll_stats = on_poll_stats
        self._poll_handles: dict[_AssignmentKey, FixedRateHandle] = {}
        self._subscriptions: dict[_AssignmentKey, SubscriptionHandle] = {}

    async def start_assignment(self, assignment: CollectionAssignment) -> None:
        """启动一个已完成 placement 的采集 assignment。"""
        key = self._key(assignment)
        if key in self._poll_handles or key in self._subscriptions:
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

            async def _acquire() -> None:
                samples = await protocol.read(points)
                values = self._interpret_batch(assignment, points, samples)
                await self._sinks.dispatch(assignment.target_sink_ids, values)

            def _on_error(exc: BaseException) -> None:
                if self._on_poll_error is not None:
                    self._on_poll_error(assignment, exc)

            def _on_stats(jitter: float, overrun: bool, missed: int) -> None:
                if self._on_poll_stats is not None:
                    self._on_poll_stats(
                        assignment,
                        jitter,
                        overrun,
                        missed,
                    )

            handle = FixedRateHandle(
                assignment.interval,
                _acquire,
                on_error=_on_error,
                on_stats=_on_stats,
            )
            await handle.start()
            self._poll_handles[key] = handle
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

        poll_handle = self._poll_handles.pop(key, None)
        if poll_handle is not None:
            await poll_handle.close()

        subscription = self._subscriptions.pop(key, None)
        if subscription is not None:
            await subscription.close()

    async def stop(self) -> None:
        """停止全部采集实例。"""
        keys = tuple({*self._poll_handles.keys(), *self._subscriptions.keys()})
        first_error: Exception | None = None
        for task_id, device_id in keys:
            try:
                await self.stop_assignment(task_id, device_id)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

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
