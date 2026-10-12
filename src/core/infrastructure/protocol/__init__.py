"""Shared Core 协议基础设施公共门面。

只暴露协议 Driver、Registry 与进程级 ADS 本机身份。
协议地址解析器和内部映射值对象保留在各协议子包，不提升为公共 API。

Driver 读写接口除点表登记的 point_id 外，也接受各协议 mapping 模块
构造的动态点对象（如 ``modbus_point``/``ads_point``/``iec104_point``），
供诊断、调试与协议白盒测试在不登记点表的情况下直接访问设备地址。
"""

from .ads import ADSDriver
from .iec104 import IEC104Driver
from .modbus import ModbusDriver

__all__ = [
    "ADSDriver",
    "IEC104Driver",
    "ModbusDriver",
]
