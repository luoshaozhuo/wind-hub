"""IEC104 从站型 Sink。

采集数据从 Runtime 经标准 SinkPort.write 进入本对象；写入时先经过
SinkReferenceExporter，再保存为按 IOA 索引的最新值快照。IEC104 主站通过
内置 server/session 执行 STARTDT、总召和只读访问。控制方向请求仍统一否定。
"""

from __future__ import annotations

from wind_hub_collector.adapter.inbound.iec104_slave.buffer import DataSnapshot
from wind_hub_collector.adapter.inbound.iec104_slave.handlers import IEC104SlaveHandlers
from wind_hub_collector.adapter.inbound.iec104_slave.server import IEC104SlaveServer
from wind_hub_collector.application.port.sink import SinkPort
from wind_hub_collector.application.sink_export import SinkReferenceExporter
from wind_hub_core.config.sinks import (
    IEC104SinkAddress,
    IEC104SinkConnection,
    ResolvedSinkConfig,
)
from wind_hub_core.model.errors import ConfigError
from wind_hub_core.model.health import HealthStatus
from wind_hub_core.model.point import PointValue


class IEC104Sink(SinkPort):
    """通过 IEC 60870-5-104 从站接口暴露 Sink 点。"""

    def __init__(self, config: ResolvedSinkConfig) -> None:
        connection = config.connection
        if not isinstance(connection, IEC104SinkConnection):
            raise ConfigError("IEC104Sink requires IEC104SinkConnection")
        for point in config.points:
            if not isinstance(point.address, IEC104SinkAddress):
                raise ConfigError(
                    f"IEC104 sink '{config.name}' point '{point.ref}' "
                    "requires IEC104SinkAddress"
                )

        self._exporter = SinkReferenceExporter(config.points)
        self._snapshot = DataSnapshot()
        self._handlers = IEC104SlaveHandlers(
            snapshot=self._snapshot,
            common_address=connection.common_address,
            batch_size=connection.batch_size,
        )
        self._server = IEC104SlaveServer(
            host=connection.host,
            port=connection.port,
            handlers=self._handlers,
            common_address=connection.common_address,
        )

    async def open(self) -> None:
        """开始监听 IEC104 主站连接。"""
        await self._server.start()

    async def close(self) -> None:
        """停止监听并关闭全部主站 session。"""
        await self._server.stop()

    async def write(self, batch: list[PointValue]) -> None:
        """更新本 Sink 配置点的最新值快照。"""
        if not batch:
            return
        self._snapshot.update(self._exporter.export(batch))

    async def flush(self) -> None:
        """IEC104 快照为内存状态，无额外 flush 动作。"""

    def health(self) -> HealthStatus:
        """返回 IEC104 server 当前监听状态。"""
        return self._server.health()

    @property
    def port(self) -> int:
        """返回实际监听端口，主要用于诊断与组件测试。"""
        return self._server.port

    @property
    def snapshot(self) -> DataSnapshot:
        """返回只读用途的最新值快照对象。"""
        return self._snapshot


__all__ = ["IEC104Sink"]
