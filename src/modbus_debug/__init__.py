"""modbus_debug — 现场 Modbus 单机调试工具（独立于 wind-hub Runtime）。

只面向 Modbus：不启动 Runtime / Scheduler / 周期采集 Task，每次 CLI
操作只连接一台指定设备，操作完成后立即断开并退出。

复用正式系统同一套配置与生产代码：
``load_config`` → ``DeviceConfig`` / ``PointConfig`` →
``ModbusDriver`` → ``Device``（读取经 Device 保证 scale/offset 与正式
采集一致）。
"""
