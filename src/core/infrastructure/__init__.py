"""Shared Core Infrastructure。

这里只承载 Collector、Commander 等应用共同复用的基础设施实现。
"""

from .config import (
    YamlCoreConfigCodec,
    YamlFileCoreConfigRepository,
    fingerprint_core_config,
)
from .protocol import (
    ADSConfig,
    ADSDriver,
    ADSLocalConfig,
    ADSLocalRouter,
    ADSPoint,
    IEC104Config,
    IEC104Driver,
    IEC104Point,
    ModbusConfig,
    ModbusDriver,
    ModbusPoint,
    ProtocolConfigValidator,
    ProtocolFactory,
    ProtocolRegistry,
    build_iec104_index,
    group_consecutive_reads,
    parse_ads_config,
    parse_ads_point,
    parse_iec104_config,
    parse_iec104_point,
    parse_modbus_config,
    parse_modbus_point,
)

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
    "ProtocolConfigValidator",
    "ProtocolFactory",
    "ProtocolRegistry",
    "YamlCoreConfigCodec",
    "build_iec104_index",
    "group_consecutive_reads",
    "parse_ads_config",
    "parse_ads_point",
    "parse_iec104_config",
    "parse_iec104_point",
    "parse_modbus_config",
    "parse_modbus_point",
    "YamlFileCoreConfigRepository",
    "fingerprint_core_config",
]
