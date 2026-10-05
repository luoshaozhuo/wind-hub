"""IEC104 mock server — 基于 c104 的最小从站。

STARTDT/TESTFR 握手、总召响应与断连语义全部由 c104/lib60870-C 承担；
本 fixture 只做点表预置与起停控制。数据点预设为
``{100: 1500.5, 200: 50.0}``，类型 ``M_ME_NC_1``（float32）。

每次 ``start()`` 重建底层 c104.Server，因此可反复起停（故障注入）；
``stop()`` 会断开全部主站连接。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import c104

logger = logging.getLogger(__name__)

IEC104_PORT = 12404


class IEC104MockServer:
    """异步生命周期的 IEC104 从站，可反复起停（供故障恢复测试）。"""

    def __init__(
        self,
        port: int = IEC104_PORT,
        common_addr: int = 1,
        data_points: dict[int, float] | None = None,
        point_type: c104.Type = c104.Type.M_ME_NC_1,
    ) -> None:
        self._port = port
        self._common_addr = common_addr
        self._data_points = dict(data_points or {100: 1500.5, 200: 50.0})
        self._point_type = point_type
        self._server: c104.Server | None = None

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> None:
        """启动从站；重复调用幂等。"""
        if self._server is not None:
            return
        server = c104.Server(ip="127.0.0.1", port=self._port)
        station = server.add_station(common_address=self._common_addr)
        assert station is not None
        for ioa, value in self._data_points.items():
            point = station.add_point(io_address=ioa, type=self._point_type)
            assert point is not None
            point.value = float(value)
        await asyncio.get_running_loop().run_in_executor(None, server.start)
        self._server = server

    async def stop(self) -> None:
        """停止从站并断开全部主站连接；重复调用幂等。"""
        server = self._server
        if server is None:
            return
        self._server = None
        await asyncio.get_running_loop().run_in_executor(None, server.stop)

    async def set_value(
        self,
        ioa: int,
        value: float,
        *,
        spontaneous: bool = False,
        quality: c104.Quality | None = None,
        recorded_at: datetime | None = None,
    ) -> None:
        """更新数据点；``spontaneous=True`` 时立即以 SPONTANEOUS 上送。

        提供 ``quality``/``recorded_at`` 时经完整 Information 写入，
        供品质位与 CP56Time2a 时标映射测试使用。
        """
        server = self._server
        if server is None:
            raise RuntimeError("IEC104MockServer is not started")
        station = server.get_station(self._common_addr)
        assert station is not None
        point = station.get_point(ioa)
        assert point is not None
        if quality is None and recorded_at is None:
            point.value = float(value)
        else:
            point.info = c104.ShortInfo(
                actual=float(value),
                quality=quality or c104.Quality(),
                recorded_at=recorded_at,
            )
        if spontaneous:
            await asyncio.get_running_loop().run_in_executor(
                None, point.transmit, c104.Cot.SPONTANEOUS
            )
