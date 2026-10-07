"""Shared Core 协议专有配置载荷。

这些字段只负责承载配置，不赋予 Application 任何 Modbus/ADS/IEC104 语义；
具体解释由对应 Infrastructure Adapter 完成。
"""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias

from ..errors import ConfigError

ProtocolOptionValue: TypeAlias = str | int | float | bool | None
ProtocolOptions: TypeAlias = Mapping[str, ProtocolOptionValue]
PointProtocolOptions: TypeAlias = Mapping[str, ProtocolOptions]


def freeze_protocol_options(
    values: Mapping[str, ProtocolOptionValue],
) -> ProtocolOptions:
    """返回协议配置的只读浅拷贝，并拒绝非有限浮点值。"""
    options = dict(values)
    for key, value in options.items():
        if not key.strip():
            raise ConfigError("protocol option keys must not be empty")
        if isinstance(value, float) and not isfinite(value):
            raise ConfigError(
                f"protocol option '{key}' must be finite"
            )
    return MappingProxyType(options)


def freeze_point_protocol_options(
    values: Mapping[str, Mapping[str, ProtocolOptionValue]],
) -> PointProtocolOptions:
    """冻结某张 PointTable 的协议专有点配置。"""
    return MappingProxyType(
        {
            point_id: freeze_protocol_options(options)
            for point_id, options in values.items()
        }
    )
