"""Domain model — device and endpoint definitions."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class Endpoint(BaseModel):
    """Connection endpoint for a device."""

    host: str
    """IP address or hostname of the device."""

    port: int
    """TCP port for the protocol connection."""

    extensions: dict[str, Any] = Field(default_factory=dict)
    """Protocol-specific parameters (e.g. IEC104 ``common_addr``,
    Modbus ``unit_id``, ADS ``ams_net_id``)."""


class DeviceInfo(BaseModel):
    """Runtime status snapshot of a device — returned by query operations."""

    device_id: str
    """Device identifier."""

    protocol: str
    """Protocol driver in use."""

    connected: bool
    """``True`` if the protocol adapter reports a healthy connection."""

    last_seen: datetime | None = None
    """UTC timestamp of the last successful read, or ``None`` if the
    device has never been read."""

    consecutive_failures: int = 0
    """Consecutive connect failures backing the reconnect backoff
    (0 when the device is healthy)."""

    last_error: str | None = None
    """Short description of the most recent failure, if any."""
