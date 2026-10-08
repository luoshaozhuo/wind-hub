"""兼容导入入口：指纹实现统一由 Core 管理。"""

from core.infrastructure.config import fingerprint_config_set

__all__ = ["fingerprint_config_set"]
