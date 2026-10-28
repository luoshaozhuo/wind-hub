"""Internal PointValue writer interface for migrated output backends."""
from __future__ import annotations

from typing import Protocol

from core.domain.point_value import PointValue


class LegacyPointSinkPort(Protocol):
    """Internal interface: new transport uses SinkPort instead."""

    async def open(self) -> None:
        """Open the target."""
        ...

    async def write(self, batch: list[PointValue]) -> None:
        """Write PointValue records."""
        ...

    async def close(self) -> None:
        """Close the target."""
        ...
