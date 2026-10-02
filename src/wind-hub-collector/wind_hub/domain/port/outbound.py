"""兼容导入层；协议能力端口的唯一实现位于 wind-hub-core。"""

from wind_hub_core.protocol.port import (
    AcquisitionMode,
    InterrogationCapable,
    ProtocolPort,
    SubscriptionHandle,
)

__all__ = [
    "AcquisitionMode",
    "InterrogationCapable",
    "ProtocolPort",
    "SubscriptionHandle",
]
