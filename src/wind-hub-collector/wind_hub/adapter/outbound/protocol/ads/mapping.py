"""兼容导入层；ADS mapping 实现已迁入 wind-hub-core。"""

from wind_hub_core.protocol.ads.mapping import ADSPoint, map_data_type, parse_point

__all__ = ["ADSPoint","map_data_type","parse_point"]
