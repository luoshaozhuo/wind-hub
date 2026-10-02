"""配置 diff 与热重载结果领域模型。"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class DeviceDiff(BaseModel):
    """新旧配置间的 Device 差异。"""

    added: list[str] = Field(default_factory=list)
    """新配置新增的 device_id。"""

    removed: list[str] = Field(default_factory=list)
    """新配置删除的 device_id。"""

    updated: list[str] = Field(default_factory=list)
    """两边都存在但配置发生变化的 device_id。"""

    unchanged: list[str] = Field(default_factory=list)
    """配置完全一致的 device_id。"""


class SinkDiff(BaseModel):
    """新旧配置间的 Sink 差异。"""

    added: list[str] = Field(default_factory=list)
    """新增 Sink 名称。"""

    removed: list[str] = Field(default_factory=list)
    """删除 Sink 名称。"""

    updated: list[str] = Field(default_factory=list)
    """配置发生变化的 Sink 名称。"""

    unchanged: list[str] = Field(default_factory=list)
    """配置不变的 Sink 名称。"""


class TaskDiff(BaseModel):
    """新旧配置间的 Task Definition 差异。"""

    added: list[str] = Field(default_factory=list)
    """新增 task_id。"""

    removed: list[str] = Field(default_factory=list)
    """删除 task_id。"""

    updated: list[str] = Field(default_factory=list)
    """配置发生变化的 task_id。"""

    unchanged: list[str] = Field(default_factory=list)
    """配置不变的 task_id。"""


class ConfigDiff(BaseModel):
    """compute_diff 生成的完整配置差异。"""

    devices: DeviceDiff = Field(default_factory=DeviceDiff)
    """Device 级变化。"""

    sinks: SinkDiff = Field(default_factory=SinkDiff)
    """Sink 级变化。"""

    tasks: TaskDiff = Field(default_factory=TaskDiff)
    """采集 Task 定义级变化——Runtime 据此重新展开 Task Instance。"""

    points_changed: bool = False
    """任一点表变化时为 True，触发 Device.set_points 重注入。"""

    point_tables_changed: list[str] = Field(default_factory=list)
    """发生变化（新增/删除/内容修改）的点表名——Runtime 据此对绑定这些表的
    设备做点映射重注入（不重建 Protocol 连接）。"""

    units_changed: bool = False
    """units 定义变化时为 True；只提交新快照，不重构运行组件。"""

    runtime_changed: bool = False
    """system.runtime 变化时为 True；当前需要重启 Collector 才能安全生效。"""

    ads_changed: bool = False
    """system.ads 本机 AMS 配置变化时为 True；进程级 ADS 身份变化需要重启。"""

    reporting_changed: bool = False
    """reporting 配置变化时为 True；IEC104 reporting server 需进程重启重建。"""

    @property
    def has_any_changes(self) -> bool:
        """任一会影响配置快照的字段发生变化时返回 True。"""
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
            or self.units_changed
            or self.runtime_changed
            or self.ads_changed
            or self.reporting_changed
        )


class ReloadResult(BaseModel):
    """一次配置热重载的结果。"""

    success: bool
    """运行时重构无错误时为 True。"""

    diff: ConfigDiff
    """本次计算并尝试应用的 ConfigDiff。"""

    errors: list[str] = Field(default_factory=list)
    """部分应用或失败时的错误明细。"""

    duration_ms: float = 0.0
    """本次 reload 墙钟耗时，毫秒。"""

    reloaded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """reload 完成时间，UTC。"""
