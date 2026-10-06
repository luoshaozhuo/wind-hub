"""Wind Hub 公共设备协议能力。

内置 ADS、Modbus、IEC104 Driver 在此导出；注册不发生在这里——组合根经
:func:`build_protocol_registry` 显式构造注册表。Driver 构造阶段不建立网络
连接，可选第三方库均在实际使用时延迟导入。
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
from wind_hub_core.protocol.registry import ProtocolRegistry, build_protocol_registry

__all__ = [
    "ADSDriver",
    "ModbusDriver",
    "IEC104Driver",
    "AcquisitionMode",
    "InterrogationCapable",
    "ProtocolPort",
    "SubscriptionHandle",
    "ProtocolRegistry",
    "build_protocol_registry",
]
