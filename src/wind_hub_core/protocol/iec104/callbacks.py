"""c104 回调适配工厂。

c104 注册回调时用 inspect 校验**精确类型注解**，因此本模块刻意：

1. 不使用 ``from __future__ import annotations``（否则注解退化为字符串，
   c104 拒绝注册）；
2. 顶层直接 ``import c104``，并只由 driver 在确认 c104 可用后延迟导入，
   不破坏「无 c104 时 wind_hub_core 可导入」的约束。

工厂把 c104 的完整回调签名收敛为 driver 真正关心的参数，driver 内部
处理器因此无需携带 c104 类型注解。
"""

from collections.abc import Callable

import c104

#: receive_callback 工厂的签名别名（driver 侧声明属性类型用）。
ReceiveCallbackFactory = Callable[
    [Callable[[c104.Point], c104.ResponseState]],
    Callable[[c104.Point, c104.Information, c104.IncomingMessage], c104.ResponseState],
]


def state_callback(
    handler: Callable[[c104.ConnectionState], None],
) -> Callable[[c104.Connection, c104.ConnectionState], None]:
    """把 ``(state)`` 处理器包装为 c104 ``on_state_change`` 回调。"""

    def callback(connection: c104.Connection, state: c104.ConnectionState) -> None:
        handler(state)

    return callback


def new_point_callback(
    handler: Callable[[c104.Station, int, c104.Type], None],
) -> Callable[[c104.Client, c104.Station, int, c104.Type], None]:
    """把 ``(station, io_address, point_type)`` 处理器包装为 c104 ``on_new_point`` 回调。"""

    def callback(
        client: c104.Client,
        station: c104.Station,
        io_address: int,
        point_type: c104.Type,
    ) -> None:
        handler(station, io_address, point_type)

    return callback


def receive_callback(
    handler: Callable[[c104.Point], c104.ResponseState],
) -> Callable[[c104.Point, c104.Information, c104.IncomingMessage], c104.ResponseState]:
    """把 ``(point)`` 处理器包装为 c104 ``on_receive`` 回调。"""

    def callback(
        point: c104.Point,
        previous_info: c104.Information,
        message: c104.IncomingMessage,
    ) -> c104.ResponseState:
        return handler(point)

    return callback
