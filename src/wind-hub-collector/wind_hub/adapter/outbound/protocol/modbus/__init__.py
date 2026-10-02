"""兼容导入层；Modbus Driver 已迁入 wind-hub-core。"""

from wind_hub_core.protocol.modbus import ModbusDriver

__all__ = ["ModbusDriver"]
