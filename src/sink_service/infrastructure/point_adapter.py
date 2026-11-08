"""Bridge the shared SinkPort to migrated task-aware target writers."""

from __future__ import annotations

from sink_service.application.point_port import PointSinkPort
from sink_service.application.point_value import SinkPointValue


class PointValueSinkAdapter:
    """Keep Sink Service's public port independent of output technology."""

    def __init__(self, target: PointSinkPort) -> None:
        self._target = target

    async def open(self) -> None:
        """Open the target writer."""
        await self._target.open()

    async def write(self, batch: list[SinkPointValue]) -> None:
        """Preserve each sample's Task mapping and target write confirmation."""
        groups: dict[str, list[SinkPointValue]] = {}
        for point in batch:
            if point.task_id is None:
                raise ValueError("Sink point is missing its source task")
            groups.setdefault(point.task_id, []).append(point)
        for task_id, values in groups.items():
            await self._target.write_task(task_id, values)

    async def close(self) -> None:
        """Release target resources."""
        await self._target.close()
