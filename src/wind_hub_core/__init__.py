"""Wind Hub 跨进程共享核心。

本包承载 Collector、Commander、Server 与运维客户端共同依赖的稳定领域模型、
静态配置语义、DeviceSession、协议 Driver/RPC 契约和主动验证能力。

具体能力从所属子包显式导入；包根不维护二次 re-export，避免形成模糊公共 API。
本包不包含 Collector Runtime、任务调度、Sink、配置写回、Web API 或进程编排，
并禁止反向依赖任何可执行组件。
"""
