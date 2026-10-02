"""兼容导入层；Point 领域模型的唯一实现位于 wind-hub-core。"""

from wind_hub_core.model.point import PointRef, PointValue, Quality

__all__ = ["PointRef", "PointValue", "Quality"]
