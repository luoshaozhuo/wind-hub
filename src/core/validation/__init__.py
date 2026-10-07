"""新 Core 模型级一致性校验。"""

from .device import validate_device_connections, validate_device_references

__all__ = [
    "validate_device_connections",
    "validate_device_references",
]
