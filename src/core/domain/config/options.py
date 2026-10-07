"""Shared Domain 配置中的不透明协议选项。"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

ProtocolOptionValue: TypeAlias = str | int | float | bool | None
ProtocolOptions: TypeAlias = Mapping[str, ProtocolOptionValue]


def freeze_protocol_options(
    values: Mapping[str, ProtocolOptionValue],
) -> ProtocolOptions:
    """返回协议配置的只读浅拷贝，并拒绝非法标量。"""
    options = dict(values)
    for key, value in options.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError("protocol option keys must be non-empty strings")
        if isinstance(value, float) and not isfinite(value):
            raise ValueError(f"protocol option '{key}' must be finite")
    return MappingProxyType(options)
