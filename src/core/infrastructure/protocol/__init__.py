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
from .builtins import build_protocol_registry
from .iec104 import (
    IEC104Config,
    IEC104Driver,
    IEC104Point,
    build_iec104_index,
    parse_iec104_config,
    parse_iec104_point,
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
from .validator import ProtocolConfigValidator

__all__ = [
    "ADSConfig",
    "ADSDriver",
    "ADSLocalConfig",
    "ADSLocalRouter",
    "ADSPoint",
    "IEC104Config",
    "IEC104Driver",
    "IEC104Point",
    "ModbusConfig",
    "ModbusDriver",
    "ModbusPoint",
    "ProtocolFactory",
    "ProtocolRegistry",
    "ProtocolConfigValidator",
    "build_iec104_index",
    "build_protocol_registry",
    "group_consecutive_reads",
    "parse_ads_config",
    "parse_ads_point",
    "parse_iec104_config",
    "parse_iec104_point",
    "parse_modbus_config",
    "parse_modbus_point",
]
