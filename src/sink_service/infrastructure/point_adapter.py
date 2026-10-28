"""Adapters for transferring the shared wire sample into existing Sink implementations.

The ownership boundary is Sink Service; target writers are migrated separately.
"""
from __future__ import annotations

from core.domain.point_value import PointValue
from core.domain.sample_value import Quality
from contracts.sample_message import SampleMessage
from sink_service.application.point_port import LegacyPointSinkPort


class PointValueSinkAdapter:
    """Translate transport samples to the current target writer contract."""

    def __init__(self, target: LegacyPointSinkPort) -> None:
        self._target = target

    async def open(self) -> None:
        await self._target.open()

    async def write(self, batch: list[SampleMessage]) -> None:
        """Write the entire batch or propagate a target failure."""
        values = [
            PointValue(
                device_id=sample.device_id,
                point_id=sample.point_id,
                value=sample.value,
                quality=Quality(sample.quality),
                timestamp=sample.sample_time,
                receive_time=sample.receive_time,
                source=sample.collector_id,
                sample_id=sample.sample_id,
            )
            for sample in batch
        ]
        for sample, point in zip(batch, values, strict=True):
            await self._target.write_task(sample.task_id, [point])

    async def close(self) -> None:
        await self._target.close()
