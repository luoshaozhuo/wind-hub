"""IEC104 从站型 Sink。

采集数据从 Runtime 经标准 SinkPort.write 进入本对象；写入时先经过
SinkReferenceExporter 完成 source/ref/scale/offset 解析，再转换为 c104
Information 更新到从站点。IEC104 主站连接、STARTDT、总召响应与点维护由
:mod:`collector.infrastructure.sink.iec104.server` 的 IEC104SlaveServer
（c104/lib60870-C）承担；本 Sink 只做 SinkPort 适配。
"""

from __future__ import annotations

from collector.application.sink_export import SinkReferenceExporter
from collector.application.sink_port import SinkPort
from collector.application.sinks import (
    IEC104SinkAddress,
    IEC104SinkConnection,
    ResolvedSinkConfig,
)
from collector.domain.point_value import PointValue
from collector.infrastructure.sink.iec104.mapping import (
    build_monitor_info,
    sink_point_type,
)
from collector.infrastructure.sink.iec104.server import IEC104SlaveServer
from core.application import ConfigError, ConnectionHealth


class IEC104Sink(SinkPort):
    """通过 IEC 60870-5-104 从站接口暴露 Sink 点。"""

    @property
    def exclusive_open(self) -> bool:
        """监听端口独占，热重载必须先关闭旧实例。"""
        return True

    def __init__(self, config: ResolvedSinkConfig) -> None:
        connection = config.connection
        if not isinstance(connection, IEC104SinkConnection):
            raise ConfigError("IEC104Sink requires IEC104SinkConnection")
        for point in config.points:
            if not isinstance(point.address, IEC104SinkAddress):
                raise ConfigError(
                    f"IEC104 sink '{config.name}' point '{point.ref}' " "requires IEC104SinkAddress"
                )

        self._exporter = SinkReferenceExporter(config.points)
        self._server = IEC104SlaveServer(
            host=connection.host,
            port=connection.port,
            common_address=connection.common_address,
        )

    async def open(self) -> None:
        """开始监听 IEC104 主站连接。"""
        await self._server.start()

    async def close(self) -> None:
        """停止监听并断开全部主站连接。"""
        await self._server.stop()

    async def write(self, batch: list[PointValue]) -> None:
        """把本 Sink 配置点的最新值更新到从站点表。"""
        if not batch:
            return
        for pv in self._exporter.export(batch):
            address = pv.definition.address
            if not isinstance(address, IEC104SinkAddress):
                continue
            self._server.update_point(
                address.ioa,
                sink_point_type(address.type_id),
                build_monitor_info(address.type_id, pv.value, pv.quality, pv.timestamp),
            )

    async def flush(self) -> None:
        """IEC104 从站点为内存状态，无额外 flush 动作。"""

    def health(self) -> ConnectionHealth:
        """返回 IEC104 server 当前监听状态。"""
        return self._server.health()

    @property
    def port(self) -> int:
        """返回配置监听端口，主要用于诊断与组件测试。"""
        return self._server.port


__all__ = ["IEC104Sink"]
