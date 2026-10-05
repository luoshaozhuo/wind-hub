"""wind-hub-server 进程宿主。

本包提供 Server 侧应用服务、端口与出站/入站适配器（FastAPI、gRPC client），
并负责服务进程级装配与生命周期；采集 Runtime 与现场协议适配器由
``wind_hub_collector`` 提供。
"""

from wind_hub_server.settings import ServerSettings

__all__ = ["ServerSettings"]
