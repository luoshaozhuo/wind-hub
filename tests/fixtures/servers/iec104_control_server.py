"""支持远控的 IEC104 mock server — 基于 c104。

在数据点（``M_ME_NC_1``）之外预置控制点（C_SC_NA_1 / C_DC_NA_1 /
C_SE_NC_1），命令确认（ACT_CON/ACT_TERM）由 lib60870-C 完成；``on_receive``
记录 ``last_control``（TypeID 名 → IOA）供测试在 Server 侧确认命令到达。

注意 c104 Station 以 IOA 为唯一键：控制点 IOA 不得与数据点 IOA 重叠。
端口在构造时分配（c104 不支持 port=0 后查询实际端口）。

c104 注册回调时校验精确类型注解，本文件刻意不使用
``from __future__ import annotations``（否则注解退化为字符串被拒绝）。
"""

import asyncio
import logging
from typing import Any

import c104

from tests.support.process import free_port

logger = logging.getLogger(__name__)

# 默认控制点：IOA → c104 控制类型。
DEFAULT_COMMAND_POINTS: dict[int, c104.Type] = {
    1001: c104.Type.C_SC_NA_1,
    1002: c104.Type.C_DC_NA_1,
    1003: c104.Type.C_SE_NC_1,
}


class IEC104ControlServer:
    """c104 从站：数据点 + 控制点，支持命令确认与 spontaneous 上送。"""

    def __init__(
        self,
        common_addr: int = 1,
        data_points: dict[int, float] | None = None,
        command_points: dict[int, c104.Type] | None = None,
        reject_ioas: set[int] | None = None,
    ) -> None:
        self._port = free_port()
        self._common_addr = common_addr
        self._data_points = dict(data_points or {100: 1500.5})
        self._command_points = dict(command_points or DEFAULT_COMMAND_POINTS)
        self._reject_ioas = set(reject_ioas or set())
        self._server: c104.Server | None = None
        self._station: c104.Station | None = None
        self._last_control: dict[str, Any] = {}

    @property
    def port(self) -> int:
        return self._port

    @property
    def last_control(self) -> dict[str, Any]:
        """TypeID 名 → 最后一次该类型控制命令的 IOA。"""
        return dict(self._last_control)

    async def start(self) -> None:
        """启动从站；重复调用幂等。"""
        if self._server is not None:
            return
        server = c104.Server(ip="127.0.0.1", port=self._port)
        station = server.add_station(common_address=self._common_addr)
        assert station is not None
        for ioa, value in self._data_points.items():
            point = station.add_point(io_address=ioa, type=c104.Type.M_ME_NC_1)
            assert point is not None
            point.value = float(value)
        for ioa, point_type in self._command_points.items():
            point = station.add_point(io_address=ioa, type=point_type)
            assert point is not None
            point.on_receive(callable=self._on_command)
        await asyncio.get_running_loop().run_in_executor(None, server.start)
        self._server = server
        self._station = station

    async def stop(self) -> None:
        """停止从站并断开全部主站连接；重复调用幂等。"""
        server = self._server
        if server is None:
            return
        self._server = None
        self._station = None
        await asyncio.get_running_loop().run_in_executor(None, server.stop)

    def _on_command(
        self,
        point: c104.Point,
        previous_info: c104.Information,
        message: c104.IncomingMessage,
    ) -> c104.ResponseState:
        """记录命令并按 ``reject_ioas`` 返回肯定/否定确认（c104 线程）。"""
        self._last_control[point.type.name] = point.io_address
        if point.io_address in self._reject_ioas:
            return c104.ResponseState.FAILURE
        return c104.ResponseState.SUCCESS

    async def set_value(self, ioa: int, value: float, *, spontaneous: bool = False) -> None:
        """更新数据点；``spontaneous=True`` 时立即以 SPONTANEOUS 上送。"""
        station = self._station
        if station is None:
            raise RuntimeError("IEC104ControlServer is not started")
        point = station.get_point(ioa)
        assert point is not None
        point.value = float(value)
        if spontaneous:
            await asyncio.get_running_loop().run_in_executor(
                None, point.transmit, c104.Cot.SPONTANEOUS
            )
