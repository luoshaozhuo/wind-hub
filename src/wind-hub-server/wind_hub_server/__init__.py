"""wind-hub-server 进程宿主。

本包位于 ``wind_hub`` 通信内核之外，只负责服务进程级装配与生命周期。
业务 Use Case、Runtime、协议适配器和 FastAPI 路由继续由 ``wind_hub`` 提供，
避免形成第二套后端实现。
"""

from wind_hub_server.settings import ServerSettings

__all__ = ["ServerSettings"]
