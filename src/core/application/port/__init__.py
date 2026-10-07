"""Shared Core Application ports。

本包只导出接口定义。
"""

from .config import (
    CoreConfigCodecPort,
    CoreConfigRepositoryPort,
    CoreConfigValidatorPort,
)
from .protocol import (
    InterrogationCapableProtocolPort,
    ProtocolFactoryPort,
    ProtocolPort,
    SubscribableProtocolPort,
    SubscriptionHandle,
)

__all__ = [
    "CoreConfigCodecPort",
    "CoreConfigRepositoryPort",
    "CoreConfigValidatorPort",
    "InterrogationCapableProtocolPort",
    "ProtocolFactoryPort",
    "ProtocolPort",
    "SubscribableProtocolPort",
    "SubscriptionHandle",
]
