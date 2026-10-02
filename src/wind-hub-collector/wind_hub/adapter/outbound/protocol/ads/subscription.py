"""兼容导入层；ADS subscription 实现已迁入 wind-hub-core。"""

from wind_hub_core.protocol.ads.subscription import ADSSubscription

__all__ = ["ADSSubscription"]
