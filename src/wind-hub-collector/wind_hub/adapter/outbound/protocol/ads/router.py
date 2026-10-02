"""兼容导入层；ADS router 实现已迁入 wind-hub-core。"""

from wind_hub_core.protocol.ads.router import ensure_local_initialized

__all__ = ["ensure_local_initialized"]
