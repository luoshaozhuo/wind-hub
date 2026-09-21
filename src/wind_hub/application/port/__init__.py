"""Application 层 outbound port——应用对外部能力的依赖边界。

这里放置由 application 层（Runtime、Use Case）消费、由 outbound adapter /
infra 实现的端口接口。被 domain 服务直接消费的扩展点端口
（``ProtocolPort`` / ``ProcessorPort``）保留在 ``domain.port.outbound``，
避免 domain 反向依赖 application。
"""
