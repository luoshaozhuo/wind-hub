"""Wind Hub 新 Collector 进程包。

周期采集与订阅推送的独立进程：DDD + Ports/Adapters，依赖方向
Infrastructure → Application → Domain，只依赖 ``core``（共享领域模型、
ProtocolPort 契约、ProtocolRegistry 与协议 Driver），不 import 任何
wind_hub_* 模块。
"""
