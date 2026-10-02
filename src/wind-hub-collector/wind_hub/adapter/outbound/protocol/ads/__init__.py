"""兼容导入层；ADS Driver 已迁入 wind-hub-core。"""

from wind_hub_core.protocol.ads import ADSDriver

__all__ = ["ADSDriver"]
