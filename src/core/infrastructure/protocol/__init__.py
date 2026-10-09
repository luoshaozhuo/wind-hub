"""Shared Core 协议基础设施公共门面。

只暴露协议 Driver、Registry 与进程级 ADS 本机身份。
协议地址解析器和内部映射值对象保留在各协议子包，不提升为公共 API。
"""

from .ads import ADSDriver, ADSLocalConfig, ADSLocalRouter
from .iec104 import IEC104Driver
from .modbus import ModbusDriver

__all__ = [
    "ADSDriver",
    "ADSLocalConfig",
    "ADSLocalRouter",
    "IEC104Driver",
    "ModbusDriver",
    "ProtocolFactory",
]
