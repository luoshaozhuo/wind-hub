"""Sink Service output interface."""

from __future__ import annotations

from typing import Protocol
from sink_service.application.point_value import SinkPointValue as PointValue



class SinkPort(Protocol):
    """Write complete groups of already-received samples."""

    async def open(self) -> None:
        """Open the target connection."""
        ...

    async def write(self, batch: list[PointValue]) -> None:
        """Return only after all writes are confirmed, or raise."""
        ...

    async def close(self) -> None:
        """Close the target connection."""
        ...
