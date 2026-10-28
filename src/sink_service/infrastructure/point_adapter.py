"""Bridge the shared SinkPort to migrated task-aware target writers."""
from __future__ import annotations

from core.domain.point_value import PointValue
from sink_service.application.point_port import LegacyPointSinkPort


class PointValueSinkAdapter:
    """Keep Sink Service's public port independent of output technology."""

    def __init__(self, target: LegacyPointSinkPort) -> None:
        self._target = target

    async def open(self) -> None:
        """Open the target writer."""
        await self._target.open()

    async def write(self, batch: list[PointValue]) -> None:
        """Preserve each sample's Task mapping and target write confirmation."""
        for point in batch:
            if point.task_id is None:
                raise ValueError("Sink point is missing its source task")
            await self._target.write_task(point.task_id, [point])

    async def close(self) -> None:
        """Release target resources."""
        await self._target.close()
