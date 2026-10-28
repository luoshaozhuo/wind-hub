"""Modbus TCP Server 型 Sink。

Runtime 通过标准 SinkPort.write 投递 PointValue；内部数据路径负责
export/scale/encode/store，TCP Server 只读暴露同一份 ModbusSinkStore。
"""

from __future__ import annotations

from core.application import ConfigError, ConnectionHealth
from sink_service.application.point_port import LegacyPointSinkPort
from core.application.sink_contract import ModbusSinkAddress, ResolvedSink, ResolvedSinkPoint
from core.domain.point_value import PointValue
from core.infrastructure.config.sink_schema import ModbusSinkConnection
from sink_service.infrastructure.adapters.modbus.pipeline import ModbusSinkDataPath
from sink_service.infrastructure.adapters.modbus.server import ModbusTcpSinkServer


class ModbusSink(LegacyPointSinkPort):
    """通过 Modbus TCP Server 对外暴露采集点。"""

    @property
    def exclusive_open(self) -> bool:
        """监听端口独占，热重载采用 close-first。"""
        return True

    def __init__(self, config: ResolvedSink) -> None:
        connection_values = dict(config.connection)
        raw_sources = config.task_sources if isinstance(config, ResolvedSink) else None
        self._task_sources = {
            task_id: {(device, point) for device, point in sources}
            for task_id, sources in (raw_sources or {}).items()
        }
        connection = ModbusSinkConnection.model_validate(connection_values)
        if not isinstance(config, ResolvedSink):
            raise ConfigError("Modbus Sink requires resolved Task output mapping")
        points = [ResolvedSinkPoint.model_validate(dict(p)) for p in config.points]
        for point in points:
            if not isinstance(point.address, ModbusSinkAddress):
                raise ConfigError(
                    f"Modbus sink '{config.sink_id}' point '{point.ref}' "
                    "requires ModbusSinkAddress"
                )

        self._data_path = ModbusSinkDataPath(points)
        self._task_points = {
            task_id: [point for point in points if point.ref in refs]
            for task_id, refs in (config.task_refs or {}).items()
        }
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

    async def write_task(self, task_id: str, batch: list[PointValue]) -> None:
        """Task 批次只允许更新本 Task 声明的共享寄存器映射。"""
        allowed = self._task_sources.get(task_id, set())
        if not allowed:
            return
        filtered = [value for value in batch if (value.device_id, value.point_id) in allowed]
        if filtered:
            self._data_path.update_selected(filtered, self._task_points.get(task_id, []))

    async def flush(self) -> None:
        """内存 datastore 无额外 flush 动作。"""

    async def health(self) -> ConnectionHealth:
        """检查 TCP Server 当前监听状态。"""
        return await self._server.health()

    @property
    def port(self) -> int:
        """返回监听端口。"""
        return self._server.port

    @property
    def data_path(self) -> ModbusSinkDataPath:
        """暴露内部数据路径供诊断与测试读取。"""
        return self._data_path


__all__ = ["ModbusSink"]
