"""兼容导入层；reporting loader 已迁入 wind-hub-core。"""

from wind_hub_core.config.reporting import load_reporting

__all__ = ["load_reporting"]
