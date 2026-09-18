"""Domain model — measurement point definitions."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Quality(str, Enum):
    """Data quality indicator for a point value."""

    GOOD = "good"
    """Measurement is within normal quality bounds."""
    BAD = "bad"
    """Measurement is unreliable or hardware reports a fault."""
    UNCERTAIN = "uncertain"
    """Quality cannot be determined — treat as suspect."""


class PointRef(BaseModel):
    """Identifies a specific point on a device, without a value.

    Used to request a read or subscribe to a point; the value is
    filled in by a ProtocolPort implementation.
    """

    device_id: str
    """Unique identifier of the device (e.g. 'turbine-01')."""

    point_id: str
    """Unique identifier of the measurement point within the device
    (e.g. 'rotor.speed', 'gen.power')."""


class PointValue(BaseModel):
    """A single collected measurement — the universal currency of the pipeline.

    Every protocol adapter produces PointValue instances.  Downstream
    processors transform and enrich them, and sinks persist or forward
    them.  This model carries *what* was measured (*value*), *where*
    it came from (*device_id* / *point_id*), *how good* it is
    (*quality*), and *when* it was captured (*timestamp*).
    """

    device_id: str
    """Device that produced this measurement."""

    point_id: str
    """Point identifier within the device."""

    value: Any
    """The measurement itself.  Type is determined by the point table
    (float, int, bool, str, …)."""

    quality: Quality = Quality.GOOD
    """Data quality of this measurement."""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """UTC timestamp when this value was collected or generated."""

    source: str | None = None
    """Protocol name that produced this value (e.g. 'ads', 'modbus')."""
