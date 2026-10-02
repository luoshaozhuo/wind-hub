"""兼容导入层；协议注册表的唯一实现位于 wind-hub-core。"""

from wind_hub_core.protocol.registry import (
    ProtocolRegistry,
    protocol_registry,
    register_protocol,
)

__all__ = ["ProtocolRegistry", "protocol_registry", "register_protocol"]
