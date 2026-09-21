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


class TaskDiff(BaseModel):
    """Per-task diff between old and new configuration."""

    added: list[str] = Field(default_factory=list)
    """Task IDs present in new config but not in old."""

    removed: list[str] = Field(default_factory=list)
    """Task IDs present in old config but not in new."""

    updated: list[str] = Field(default_factory=list)
    """Task IDs that exist in both but with changed configuration."""

    unchanged: list[str] = Field(default_factory=list)
    """Task IDs with identical configuration."""


class ConfigDiff(BaseModel):
    """Full configuration diff produced by :func:`compute_diff`."""

    devices: DeviceDiff = Field(default_factory=DeviceDiff)
    """Device-level changes."""

    sinks: SinkDiff = Field(default_factory=SinkDiff)
    """Sink-level changes."""

    tasks: TaskDiff = Field(default_factory=TaskDiff)
    """采集 Task 定义级变化——Runtime 据此重新展开 Task Instance。"""

    points_changed: bool = False
    """``True`` when any point table changed — 触发点映射重注入与处理链重建。"""

    point_tables_changed: list[str] = Field(default_factory=list)
    """发生变化（新增/删除/内容修改）的点表名——Runtime 据此对绑定这些表的
    设备做点映射重注入（不重建 Protocol 连接）。"""

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
            or self.tasks.added
            or self.tasks.removed
            or self.tasks.updated
            or self.points_changed
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
