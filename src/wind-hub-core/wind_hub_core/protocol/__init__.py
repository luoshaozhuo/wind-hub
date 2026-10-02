"""Wind Hub 公共设备协议能力与内置 Driver 注册入口。

导入本模块会加载内置 ADS、Modbus、IEC104 Driver 并完成进程级协议工厂注册；
Driver 构造阶段不建立网络连接，可选第三方库均在实际使用时延迟导入。
"""

from wind_hub_core.protocol.ads.driver import ADSDriver
from wind_hub_core.protocol.iec104.driver import IEC104Driver
from wind_hub_core.protocol.modbus.driver import ModbusDriver
from wind_hub_core.protocol.port import (
    AcquisitionMode,
    InterrogationCapable,
    ProtocolPort,
    SubscriptionHandle,
)
from wind_hub_core.protocol.registry import ProtocolRegistry, protocol_registry, register_protocol

__all__ = [
    "ADSDriver",
    "ModbusDriver",
    "IEC104Driver",
    "AcquisitionMode",
    "InterrogationCapable",
    "ProtocolPort",
    "SubscriptionHandle",
    "ProtocolRegistry",
    "protocol_registry",
    "register_protocol",
]
