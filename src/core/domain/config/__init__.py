"""Shared Domain 配置模型与规则。"""

from .snapshot import (
    CoreConfigSnapshot,
    ProtocolOptions,
    ProtocolOptionValue,
)
from .validation import validate_core_config

__all__ = [
    "CoreConfigSnapshot",
    "ProtocolOptionValue",
    "ProtocolOptions",
    "validate_core_config",
]
