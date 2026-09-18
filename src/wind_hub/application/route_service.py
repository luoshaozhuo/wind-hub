"""Route query service — 只读路由查询的实现。

将 :class:`~wind_hub.domain.engine.scheduler.Scheduler` 包装为
:class:`~wind_hub.domain.port.inbound.RouteQueryUseCase`，供 CLI / Web API
适配器查询路由决策与未匹配点。服务本身不做任何路由计算，只做惰性委托。

持有 Scheduler（而非直接持有 Router）是因为热重载可能通过
:meth:`Scheduler.replace_router` 替换路由表——通过
:meth:`Scheduler.current_router` 读取即可始终看到最新实例，无需在每次热重载后
重组服务引用。
"""

from __future__ import annotations

from wind_hub.domain.engine.scheduler import Scheduler
from wind_hub.domain.model.route import RouteDecision
from wind_hub.domain.port.inbound import RouteQueryUseCase


class RouteService(RouteQueryUseCase):
    """只读路由查询服务，委托给持有的 :class:`Scheduler` 当前路由表。

    通过 :meth:`Scheduler.current_router` 间接访问路由表，做到热重载感知：
    无论路由表是否在运行时被替换，查询都基于最新快照。
    """

    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler

    def explain(self, device_id: str, point_id: str) -> RouteDecision:
        """解释某点位的路由决策（同步，纯内存查询）。

        Args:
            device_id: 设备标识。
            point_id: 点位标识。

        Returns:
            :class:`RouteDecision`，``source`` 与 ``targets`` 由路由表给出。
        """
        return self._scheduler.current_router.explain(device_id, point_id)

    def unmatched_points(self) -> list[tuple[str, str]]:
        """返回所有未匹配到任何 sink 的 ``(device_id, point_id)`` 键。"""
        return self._scheduler.current_router.unmatched_points()
