"""modbus_debug — 现场 Modbus 单机调试工具。

固定读取 ``configs/site_wtg_modbus`` 正式配置，但通信直接使用 pymodbus
``AsyncModbusTcpClient``，不经 wind-hub Runtime / ModbusDriver，用于独立
验证现场 PLC 与 site_wtg_modbus 配置。
"""
