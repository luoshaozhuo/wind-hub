"""Internal SinkPointValue writer interface for migrated output backends."""

from __future__ import annotations

from typing import Protocol
from sink_service.application.point_value import SinkPointValue



class LegacyPointSinkPort(Protocol):
    """Internal interface: new transport uses SinkPort instead."""

    async def open(self) -> None:
        """Open the target."""
        ...

    async def write(self, batch: list[SinkPointValue]) -> None:
        """Write SinkPointValue records."""
        ...

    async def write_task(self, task_id: str, batch: list[SinkPointValue]) -> None:
        """Write only point values routed for an acquisition task."""
        ...

    async def close(self) -> None:
        """Close the target."""
        ...
