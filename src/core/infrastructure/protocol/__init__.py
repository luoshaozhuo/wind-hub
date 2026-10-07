"""Shared Core 协议基础设施公共门面。

只暴露协议 Driver、显式 Registry、静态校验器以及进程级 ADS 本机身份。
协议地址解析器和内部映射值对象保留在各协议子包，不提升为公共 API。
"""

from .ads import ADSDriver, ADSLocalConfig, ADSLocalRouter
from .builtins import build_protocol_registry
from .iec104 import IEC104Driver
from .modbus import ModbusDriver
from .registry import ProtocolFactory, ProtocolRegistry
from .validator import ProtocolConfigValidator

__all__ = [
    "ADSDriver",
    "ADSLocalConfig",
    "ADSLocalRouter",
    "IEC104Driver",
    "ModbusDriver",
    "ProtocolConfigValidator",
    "ProtocolFactory",
    "ProtocolRegistry",
    "build_protocol_registry",
]
