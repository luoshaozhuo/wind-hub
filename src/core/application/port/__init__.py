"""Shared Core Application ports。

本包只导出 Application 对外部能力的接口定义。
"""

from .config import (
    CoreConfigCodecPort,
    CoreConfigRepositoryPort,
    CoreConfigValidatorPort,
)
from .protocol import (
    InterrogatableProtocolPort,
    ProtocolPort,
    ProtocolSampleCallback,
    SubscribableProtocolPort,
    SubscriptionHandle,
)

__all__ = [
    "CoreConfigCodecPort",
    "CoreConfigRepositoryPort",
    "CoreConfigValidatorPort",
    "InterrogatableProtocolPort",
    "ProtocolPort",
    "ProtocolSampleCallback",
    "SubscribableProtocolPort",
    "SubscriptionHandle",
]
