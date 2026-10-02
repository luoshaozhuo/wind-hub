"""兼容导入层；Modbus mapping 实现已迁入 wind-hub-core。"""

from wind_hub_core.protocol.modbus.mapping import ModbusPoint, parse_point

__all__ = ["ModbusPoint","parse_point"]
