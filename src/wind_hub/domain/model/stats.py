"""Domain model — runtime statistics."""

from __future__ import annotations

from pydantic import BaseModel


class SchedulerStats(BaseModel):
    """Scheduler runtime statistics, exposed via health/status queries."""

    devices_total: int = 0
    """Total number of configured devices."""

    devices_connected: int = 0
    """Number of devices with a healthy connection."""

    points_collected: int = 0
    """Cumulative raw point values collected from devices."""

    points_routed: int = 0
    """Cumulative point values successfully routed to sinks."""

    points_unmatched: int = 0
    """Cumulative point values with no route target."""

    points_dropped: int = 0
    """Cumulative point values dropped due to back-pressure."""

    sinks_total: int = 0
    """Total number of configured sinks."""

    sinks_healthy: int = 0
    """Number of sinks reporting healthy."""


class PipelineStats(BaseModel):
    """Pipeline runtime statistics."""

    processors_total: int = 0
    """Number of processors in the pipeline."""

    batches_processed: int = 0
    """Cumulative batches processed."""

    processor_errors: int = 0
    """Cumulative processor error count."""
