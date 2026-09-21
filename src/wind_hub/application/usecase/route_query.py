"""Route query use case——只读路由查询的应用编排。

基于 :class:`~wind_hub.application.runtime.runtime.Runtime`，供 CLI /
Web API 适配器查询路由决策与未匹配点。用例本身不做任何路由计算，只做
惰性委托。

持有 Runtime（而非直接持有 Router）是因为热重载可能通过
:meth:`Runtime.replace_router` 替换路由表——通过 :meth:`Runtime.current_router`
读取即可始终看到最新实例，无需在每次热重载后重组用例引用。

路由表是纯内存结构、无 I/O，因此方法设计为同步，与 :class:`Router`
的纯逻辑语义一致，避免引入不必要的 ``asyncio`` 边界。
"""

from __future__ import annotations

from wind_hub.application.runtime.runtime import Runtime
from wind_hub.domain.model.route import RouteDecision


class RouteQueryUseCase:
    """只读路由查询用例，委托给 Runtime 的当前路由表。

    通过 :meth:`Runtime.current_router` 间接访问路由表，做到热重载感知：
    无论路由表是否在运行时被替换，查询都基于最新快照。
    """

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    def explain(self, device_id: str, point_id: str) -> RouteDecision:
        """解释某点位的路由决策（同步，纯内存查询）。

        Args:
            device_id: 设备标识。
            point_id: 点位标识。

        Returns:
            :class:`RouteDecision`，``source`` 与 ``targets`` 由路由表给出。
        """
        return self._runtime.current_router.explain(device_id, point_id)

    def unmatched_points(self) -> list[tuple[str, str]]:
        """返回所有未匹配到任何 sink 的 ``(device_id, point_id)`` 键。"""
        return self._runtime.current_router.unmatched_points()
