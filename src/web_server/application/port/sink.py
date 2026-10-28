"""Application boundary for independently deployed Sink Service management."""
from __future__ import annotations

from typing import Any, Protocol


class SinkDirectoryPort(Protocol):
    """Read and control Sinks without knowing transport endpoints."""

    async def list_sinks(self) -> list[dict[str, Any]]:
        """Return live Sink state across configured service processes."""
        ...

    async def call(self, method: str, name: str) -> dict[str, Any]:
        """Invoke exactly one Sink's management operation."""
        ...
