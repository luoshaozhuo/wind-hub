"""modbus_debug — 现场 Modbus 单机调试工具（最简版）。

机组与点表以字面量维护在 cli.py 文件头（DEVICES / POINTS），不依赖
wind-hub 配置与生产代码；每次命令只连接一台设备，读完即断开。
"""
