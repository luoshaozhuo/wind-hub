"""Sink Service output interface."""
from __future__ import annotations

from typing import Protocol

from contracts.sample_message import SampleMessage


class SinkPort(Protocol):
    """Write complete groups of already-received samples."""

    async def open(self) -> None:
        """Open the target connection."""
        ...

    async def write(self, batch: list[SampleMessage]) -> None:
        """Return only after all writes are confirmed, or raise."""
        ...

    async def close(self) -> None:
        """Close the target connection."""
        ...
