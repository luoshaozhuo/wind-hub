"""c104 精确类型回调适配工厂。

本模块故意不启用 postponed annotations。c104 注册回调时会检查真实类型注解，
因此只由 IEC104Driver 在确认可选依赖已安装后延迟导入。
"""

from collections.abc import Callable

import c104


def state_callback(
    handler: Callable[[c104.ConnectionState], None],
) -> Callable[[c104.Connection, c104.ConnectionState], None]:
    """把简化状态处理器包装成 c104 on_state_change 回调。"""

    def callback(
        connection: c104.Connection,
        state: c104.ConnectionState,
    ) -> None:
        handler(state)

    return callback


def new_point_callback(
    handler: Callable[[c104.Station, int, c104.Type], None],
) -> Callable[[c104.Client, c104.Station, int, c104.Type], None]:
    """把简化新点处理器包装成 c104 on_new_point 回调。"""

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
) -> Callable[
    [c104.Point, c104.Information, c104.IncomingMessage],
    c104.ResponseState,
]:
    """把简化点接收处理器包装成 c104 on_receive 回调。"""

    def callback(
        point: c104.Point,
        previous_info: c104.Information,
        message: c104.IncomingMessage,
    ) -> c104.ResponseState:
        return handler(point)

    return callback
