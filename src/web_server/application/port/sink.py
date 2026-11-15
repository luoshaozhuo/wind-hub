"""Application port for authenticated independent Sink Delivery management."""

from __future__ import annotations

from typing import Any, Protocol


class SinkDirectoryPort(Protocol):
    """Query and control authoritative Deliveries without transport knowledge."""

    async def list_deliveries(self) -> list[dict[str, Any]]:
        """Return Delivery states, rejecting duplicate active owners."""
        ...

    async def control_delivery(self, delivery_id: str, *, start: bool) -> dict[str, Any]:
        """Persist a revision-checked Delivery START or STOP intent."""
        ...
