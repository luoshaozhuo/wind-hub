"""Shared Domain 配置模型与规则。"""

from .diff import CoreConfigDiff, IndexDiff, compute_core_config_diff
from .options import (
    ProtocolOptions,
    ProtocolOptionValue,
    freeze_protocol_options,
)
from .snapshot import CoreConfigSnapshot
from .validation import validate_core_config

__all__ = [
    "CoreConfigDiff",
    "CoreConfigSnapshot",
    "IndexDiff",
    "ProtocolOptionValue",
    "ProtocolOptions",
    "compute_core_config_diff",
    "freeze_protocol_options",
    "validate_core_config",
]
