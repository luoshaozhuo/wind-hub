"""设备协议适配器注册入口。

导入各协议 driver 仅用于触发 protocol_registry 自注册；这里不创建连接，也不
执行网络 I/O。
"""

from wind_hub.adapter.outbound.protocol.ads.driver import ADSDriver  # noqa: F401
from wind_hub.adapter.outbound.protocol.iec104.driver import IEC104Driver  # noqa: F401
from wind_hub.adapter.outbound.protocol.modbus.driver import ModbusDriver  # noqa: F401

__all__ = ["ADSDriver", "IEC104Driver", "ModbusDriver"]
