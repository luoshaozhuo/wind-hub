"""Collector 专属运行态模型入口。"""

from wind_hub_collector.domain.model.device import DeviceInfo
from wind_hub_collector.domain.model.errors import SinkError

__all__ = ["DeviceInfo", "SinkError"]
