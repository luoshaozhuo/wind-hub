"""兼容导入层；ADS config 实现已迁入 wind-hub-core。"""

from wind_hub_core.protocol.ads.config import ADSConfig, from_device_config

__all__ = ["ADSConfig","from_device_config"]
