"""兼容导入层；Modbus config 实现已迁入 wind-hub-core。"""

from wind_hub_core.protocol.modbus.config import ModbusConfig, from_device_config

__all__ = ["ModbusConfig","from_device_config"]
