"""兼容导入层；配置 diff/reload 共享模型已迁入 wind-hub-core。"""

from wind_hub_core.model.reload import (
    ConfigDiff,
    DeviceDiff,
    ReloadResult,
    SinkDiff,
    TaskDiff,
)

__all__ = ["DeviceDiff", "SinkDiff", "TaskDiff", "ConfigDiff", "ReloadResult"]
