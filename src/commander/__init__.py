"""新 Commander 进程包——即时设备操作（read / write / diagnostics）。

依赖 Shared Core（``core``）的领域模型、协议契约与协议 Driver；
不依赖 Collector / Web Server 进程包。

部署约束：Commander 与 Collector 是独立进程、各自与设备建连，对同一
设备无访问仲裁。对 Modbus / IEC104 等单主站协议，现场部署必须保证
Commander 即时操作与 Collector 周期采集不互相干扰（如独立链路或
设备侧支持多主站）；本进程不提供设备级互斥。
"""
