"""Modbus TCP Server 型 Sink。

Runtime 通过标准 SinkPort.write 投递 PointValue；内部数据路径负责
export/scale/encode/store，TCP Server 只读暴露同一份 ModbusSinkStore。
"""

from __future__ import annotations

from collector.application.sink_port import SinkPort
from collector.domain.point_value import PointValue
from collector.infrastructure.sink.modbus.pipeline import ModbusSinkDataPath
from collector.infrastructure.sink.modbus.server import ModbusTcpSinkServer
from core.application import ConfigError, ConnectionHealth
from core.application.sink_config import (
    ModbusSinkAddress,
    ModbusSinkConnection,
    ResolvedSinkConfig,
)


class ModbusSink(SinkPort):
    """通过 Modbus TCP Server 对外暴露采集点。"""

    @property
    def exclusive_open(self) -> bool:
        """监听端口独占，热重载采用 close-first。"""
        return True

    def __init__(self, config: ResolvedSinkConfig) -> None:
        connection = config.connection
        if not isinstance(connection, ModbusSinkConnection):
            raise ConfigError("ModbusSink requires ModbusSinkConnection")
        for point in config.points:
            if not isinstance(point.address, ModbusSinkAddress):
                raise ConfigError(
                    f"Modbus sink '{config.name}' point '{point.ref}' " "requires ModbusSinkAddress"
                )

        self._data_path = ModbusSinkDataPath(config.points)
        self._server = ModbusTcpSinkServer(connection, self._data_path)

    async def open(self) -> None:
        """开始监听 Modbus TCP 主站连接。"""
        await self._server.start()

    async def close(self) -> None:
        """停止监听并释放端口。"""
        await self._server.stop()

    async def write(self, batch: list[PointValue]) -> None:
        """把 Runtime 点值批次更新到 Modbus datastore。"""
        if not batch:
            return
        self._data_path.update(batch)

    async def flush(self) -> None:
        """内存 datastore 无额外 flush 动作。"""

    def health(self) -> ConnectionHealth:
        """返回 TCP Server 当前健康状态。"""
        return self._server.health()

    @property
    def port(self) -> int:
        """返回监听端口。"""
        return self._server.port

    @property
    def data_path(self) -> ModbusSinkDataPath:
        """暴露内部数据路径供诊断与测试读取。"""
        return self._data_path


__all__ = ["ModbusSink"]
