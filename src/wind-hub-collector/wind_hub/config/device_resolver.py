"""兼容导入层；设备配置解析实现已迁入 wind-hub-core。"""

from wind_hub_core.config.device_resolver import resolve_devices

__all__ = ["resolve_devices"]
