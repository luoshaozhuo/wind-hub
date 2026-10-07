"""共享设备通信会话。

DeviceSession 是 Collector 与 Commander 共用的单连接通信门面。它绑定一个
DeviceConnection、对应 Device/PointTable 与 ProtocolPort，只负责连接、即时读写
和协议样本归一化，不承担轮询、订阅、重连、Task 或 Sink 生命周期。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from core.domain import ConnectionId, DeviceId, PointAccess, ProtocolPoint

from .config import CoreConfigSnapshot, DeviceConnection
from .errors import ConfigError, ProtocolError
from .interpretation import interpret_protocol_sample, prepare_protocol_write
from .measurement import PointValue, PointWrite
from .port import (
    ConnectionHealth,
    ProtocolFactoryPort,
    ProtocolPort,
    ProtocolWrite,
    ProtocolWriteResult,
)


class DeviceSession:
    """一条 DeviceConnection 的共享通信会话。"""

    def __init__(
        self,
        snapshot: CoreConfigSnapshot,
        connection: DeviceConnection,
        protocol: ProtocolPort,
    ) -> None:
        self._snapshot = snapshot
        self._connection = connection
        self._protocol = protocol

        device = snapshot.devices[connection.device_id]
        model = snapshot.device_models[device.device_model_id]
        self._point_table = snapshot.point_tables[model.point_table_id]

    @property
    def connection_id(self) -> ConnectionId:
        """返回当前静态连接身份。"""
        return self._connection.connection_id

    @property
    def device_id(self) -> DeviceId:
        """返回当前连接所属设备。"""
        return self._connection.device_id

    @property
    def protocol(self) -> ProtocolPort:
        """返回底层协议端口。"""
        return self._protocol

    async def connect(self) -> None:
        """建立底层协议连接。"""
        await self._protocol.connect()

    async def close(self) -> None:
        """关闭底层协议连接。"""
        await self._protocol.close()

    def health(self) -> ConnectionHealth:
        """返回底层协议缓存的连接状态。"""
        return self._protocol.health()

    async def read(
        self,
        point_ids: Sequence[str],
        *,
        observed_at: datetime | None = None,
    ) -> tuple[PointValue, ...]:
        """按 PointTable 本地点 ID 批量读取并返回标准业务值。"""
        normalized_ids = tuple(point_id.strip() for point_id in point_ids)
        _require_unique_point_ids(normalized_ids)
        points = tuple(self._point_table.point(point_id) for point_id in normalized_ids)
        for point in points:
            if point.access not in (PointAccess.READ, PointAccess.READ_WRITE):
                raise ConfigError(f"point '{point.point_id}' is not readable")

        samples = await self._protocol.read(points)

        expected_ids = {point.point_id for point in points}
        actual_ids = {sample.point_id for sample in samples}
        if actual_ids != expected_ids or len(samples) != len(points):
            raise ProtocolError(
                f"connection '{self.connection_id}' returned unexpected point set"
            )

        timestamp = observed_at or datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ConfigError("observed_at must be timezone-aware")

        samples_by_id = {sample.point_id: sample for sample in samples}
        return tuple(
            interpret_protocol_sample(
                self._snapshot,
                self.device_id,
                samples_by_id[point_id],
                observed_at=timestamp,
            )
            for point_id in normalized_ids
        )

    async def write(
        self,
        writes: Sequence[PointWrite],
    ) -> tuple[ProtocolWriteResult, ...]:
        """按标准业务值执行批量写入。

        本方法只负责点访问权限与值映射；控制权限、幂等、超时、联锁和审计由
        Commander Application 负责。
        """
        _require_unique_point_ids(tuple(write.point_id for write in writes))
        protocol_writes: list[ProtocolWrite] = []
        for write in writes:
            point = self._point_table.point(write.point_id)
            if point.access not in (PointAccess.WRITE, PointAccess.READ_WRITE):
                raise ConfigError(f"point '{point.point_id}' is not writable")
            business_point = self._snapshot.business_points[point.business_point_id]
            protocol_writes.append(
                ProtocolWrite(
                    point=point,
                    value=prepare_protocol_write(
                        business_point,
                        point,
                        write.value,
                    ),
                )
            )
        results = await self._protocol.write(protocol_writes)
        expected_ids = tuple(write.point.point_id for write in protocol_writes)
        if (
            len(results) != len(expected_ids)
            or {result.point_id for result in results} != set(expected_ids)
        ):
            raise ProtocolError(
                f"connection '{self.connection_id}' returned unexpected write results"
            )
        results_by_id = {result.point_id: result for result in results}
        return tuple(results_by_id[point_id] for point_id in expected_ids)

    def point(self, point_id: str) -> ProtocolPoint:
        """返回当前设备 PointTable 中的协议点。"""
        return self._point_table.point(point_id)


def create_device_session(
    snapshot: CoreConfigSnapshot,
    connection_id: ConnectionId,
    protocols: ProtocolFactoryPort,
) -> DeviceSession:
    """按共享配置和协议工厂创建尚未连接的 DeviceSession。"""
    connection = snapshot.device_connections[connection_id]
    if not connection.enabled:
        raise ConfigError(f"connection '{connection_id}' is disabled")
    device = snapshot.devices[connection.device_id]
    model = snapshot.device_models[device.device_model_id]
    point_table = snapshot.point_tables[model.point_table_id]

    protocol = protocols.create(connection, point_table)
    return DeviceSession(snapshot, connection, protocol)



def _require_unique_point_ids(point_ids: Sequence[str]) -> None:
    """拒绝一次操作内重复点，避免重复读写和写入顺序歧义。"""
    if any(not point_id for point_id in point_ids):
        raise ConfigError("point_id must not be empty")
    if len(point_ids) != len(set(point_ids)):
        raise ConfigError("point_ids must not contain duplicates")
