"""兼容导入层；ADS driver 实现已迁入 wind-hub-core。"""

from wind_hub_core.protocol.ads.driver import ADSDriver

__all__ = ["ADSDriver"]
