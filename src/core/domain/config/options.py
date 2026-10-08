"""协议专有连接参数的值类型与冻结校验。"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

ProtocolOptionValue: TypeAlias = str | int | float | bool | None
ProtocolOptions: TypeAlias = Mapping[str, ProtocolOptionValue]


def freeze_protocol_options(values: Mapping[str, ProtocolOptionValue]) -> ProtocolOptions:
    """复制并冻结协议参数，校验键非空、浮点值有限。"""
    options = dict(values)
    for key, value in options.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("protocol option keys must be non-empty strings")
        if isinstance(value, float) and not isfinite(value):
            raise ValueError(f"protocol option '{key}' must be finite")
    return MappingProxyType(options)


__all__ = ["ProtocolOptionValue", "ProtocolOptions", "freeze_protocol_options"]
