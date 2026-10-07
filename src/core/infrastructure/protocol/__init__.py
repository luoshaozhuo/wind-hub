"""Shared Core 协议基础设施。"""

from .ads import (
    ADSConfig,
    ADSDriver,
    ADSLocalConfig,
    ADSLocalRouter,
    ADSPoint,
    parse_ads_config,
    parse_ads_point,
)
from .modbus import (
    ModbusConfig,
    ModbusDriver,
    ModbusPoint,
    group_consecutive_reads,
    parse_modbus_config,
    parse_modbus_point,
)
from .registry import ProtocolFactory, ProtocolRegistry

__all__ = [
    "ADSConfig",
    "ADSDriver",
    "ADSLocalConfig",
    "ADSLocalRouter",
    "ADSPoint",
    "ModbusConfig",
    "ModbusDriver",
    "ModbusPoint",
    "ProtocolFactory",
    "ProtocolRegistry",
    "group_consecutive_reads",
    "parse_ads_config",
    "parse_ads_point",
    "parse_modbus_config",
    "parse_modbus_point",
]
