"""Domain model — configuration diff and reload result."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class DeviceDiff(BaseModel):
    """Per-device diff between old and new configuration."""

    added: list[str] = Field(default_factory=list)
    """Device IDs present in new config but not in old."""

    removed: list[str] = Field(default_factory=list)
    """Device IDs present in old config but not in new."""

    updated: list[str] = Field(default_factory=list)
    """Device IDs that exist in both but with changed configuration."""

    unchanged: list[str] = Field(default_factory=list)
    """Device IDs with identical configuration."""


class SinkDiff(BaseModel):
    """Per-sink diff between old and new configuration."""

    added: list[str] = Field(default_factory=list)
    """Sink names present in new config but not in old."""

    removed: list[str] = Field(default_factory=list)
    """Sink names present in old config but not in new."""

    updated: list[str] = Field(default_factory=list)
    """Sink names that exist in both but with changed configuration."""

    unchanged: list[str] = Field(default_factory=list)
    """Sink names with identical configuration."""


class ConfigDiff(BaseModel):
    """Full configuration diff produced by :func:`compute_diff`."""

    devices: DeviceDiff = Field(default_factory=DeviceDiff)
    """Device-level changes."""

    sinks: SinkDiff = Field(default_factory=SinkDiff)
    """Sink-level changes."""

    points_changed: bool = False
    """``True`` when the points list has changed — triggers routing table rebuild."""

    rules_changed: bool = False
    """``True`` when routing rules have changed — triggers routing table rebuild."""

    pipeline_changed: bool = False
    """``True`` when the processor pipeline has changed."""

    @property
    def has_any_changes(self) -> bool:
        """``True`` if any diff field indicates a change."""
        return bool(
            self.devices.added
            or self.devices.removed
            or self.devices.updated
            or self.sinks.added
            or self.sinks.removed
            or self.sinks.updated
            or self.points_changed
            or self.rules_changed
            or self.pipeline_changed
        )


class ReloadResult(BaseModel):
    """Outcome of a configuration hot-reload."""

    success: bool
    """``True`` when the reload completed without errors."""

    diff: ConfigDiff
    """The computed diff that was applied."""

    errors: list[str] = Field(default_factory=list)
    """Error details when ``success`` is ``False``."""

    duration_ms: float = 0.0
    """Wall-clock time the reload operation took, in milliseconds."""

    reloaded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """UTC timestamp when the reload completed."""
