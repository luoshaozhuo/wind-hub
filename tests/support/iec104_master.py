"""IEC104 测试主站——基于 c104 的最小 master。

供需要验证 IEC104 从站（Sink / slave server）协议行为的测试使用：
建连、总召（C_IC_NA_1，QOI=20）、值回收。wire 编解码与握手全部由
c104/lib60870-C 承担，不经过被测的 server 适配层，保证验证链路独立。

注意：c104 注册回调时校验精确类型注解，本文件刻意不使用
``from __future__ import annotations``（否则注解退化为字符串被拒绝）。
"""

import asyncio
from enum import IntEnum
from typing import Any

import c104

from tests.support.wait import wait_until


def value_from_c104(value: Any) -> Any:
    """把 c104 点值转换为标量（Double/Step 等 IntEnum 转 int）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, IntEnum):
        return int(value)
    if isinstance(value, str | int | float):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class IEC104MasterClient:
    """最小 IEC104 主站：connect → 总召 → 按 IOA 读取镜像值。"""

    def __init__(self, port: int, host: str = "127.0.0.1", common_addr: int = 1) -> None:
        self._common_addr = common_addr
        self._client = c104.Client()
        connection = self._client.add_connection(
            ip=host, port=port, init=c104.Init.NONE
        )
        assert connection is not None
        self._connection = connection
        station = connection.add_station(common_address=common_addr)
        assert station is not None
        self._station = station
        # 本连接周期内已收到数据的 IOA（总召/自发）。
        self._received: set[int] = set()
        self._client.on_new_point(callable=self._on_new_point)

    def _on_new_point(
        self,
        client: c104.Client,
        station: c104.Station,
        io_address: int,
        point_type: c104.Type,
    ) -> None:
        point = station.add_point(io_address=io_address, type=point_type)
        if point is not None:
            point.on_receive(callable=self._on_receive)

    def _on_receive(
        self,
        point: c104.Point,
        previous_info: c104.Information,
        message: c104.IncomingMessage,
    ) -> c104.ResponseState:
        self._received.add(point.io_address)
        return c104.ResponseState.NONE

    async def connect(self) -> None:
        """启动客户端并等待连接进入 OPEN。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._client.start)
        self._connection.connect()
        await wait_until(
            lambda: True if self._connection.state == c104.ConnectionState.OPEN else None,
            timeout=10.0,
            description="iec104 master connection OPEN",
        )

    async def close(self) -> None:
        """停止客户端并断开连接。"""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._client.stop)

    async def interrogate(self, expected: int | None = None) -> dict[int, Any]:
        """发起站总召，返回 {ioa: value}。

        Args:
            expected: 期望收到的点数；提供时等待收齐再返回。
        """
        loop = asyncio.get_running_loop()
        accepted = await loop.run_in_executor(
            None,
            lambda: self._connection.interrogation(
                common_address=self._common_addr,
                cause=c104.Cot.ACTIVATION,
                qualifier=c104.Qoi.STATION,
            ),
        )
        assert accepted, "interrogation was not accepted by the server"
        if expected is not None:
            await wait_until(
                lambda: self._values() if len(self._received) >= expected else None,
                timeout=10.0,
                description=f"iec104 master received {expected} points",
            )
        return self._values()

    def _values(self) -> dict[int, Any]:
        values: dict[int, Any] = {}
        for ioa in self._received:
            point = self._station.get_point(ioa)
            if point is not None:
                values[ioa] = value_from_c104(point.value)
        return values
