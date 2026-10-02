"""Wind Hub 公共设备协议能力与内置 Driver 注册入口。"""

from wind_hub_core.protocol.port import (
    AcquisitionMode,
    InterrogationCapable,
    ProtocolPort,
    SubscriptionHandle,
)
from wind_hub_core.protocol.registry import ProtocolRegistry, protocol_registry, register_protocol

__all__ = [
    "AcquisitionMode",
    "InterrogationCapable",
    "ProtocolPort",
    "SubscriptionHandle",
    "ProtocolRegistry",
    "protocol_registry",
    "register_protocol",
]
