"""Shared Core Application ports。

本包只导出 Application 对外部能力的接口定义。
"""

from .config import (
    CoreConfigCodecPort,
    CoreConfigRepositoryPort,
    CoreConfigValidatorPort,
)
from .protocol import ProtocolPort, ProtocolSampleCallback, SubscriptionHandle

__all__ = [
    "CoreConfigCodecPort",
    "CoreConfigRepositoryPort",
    "CoreConfigValidatorPort",
    "ProtocolPort",
    "ProtocolSampleCallback",
    "SubscriptionHandle",
]
