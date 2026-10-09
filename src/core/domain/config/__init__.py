"""Shared Domain 配置模型与规则。"""

from .lookups import point_table_for_device, protocol_options_for
from .options import (
    ProtocolOptions,
    ProtocolOptionValue,
    freeze_protocol_options,
)
from .validation import validate_core_config

__all__ = [
    "ProtocolOptionValue",
    "ProtocolOptions",
    "protocol_options_for",
    "freeze_protocol_options",
    "point_table_for_device",
    "validate_core_config",
]
