"""Shared Core Application ports。

本包只导出 Application 对上层用例和下层外部能力的接口定义。
"""

from .config import (
    CoreConfigCodecPort,
    CoreConfigPort,
    CoreConfigRepositoryPort,
    CoreConfigValidatorPort,
)
from .protocol import ProtocolPort, ProtocolSampleCallback, SubscriptionHandle

__all__ = [
    "CoreConfigCodecPort",
    "CoreConfigPort",
    "CoreConfigRepositoryPort",
    "CoreConfigValidatorPort",
    "ProtocolPort",
    "ProtocolSampleCallback",
    "SubscriptionHandle",
]
