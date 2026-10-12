"""Commander 激活配置的只读测点元数据查询。"""

from __future__ import annotations

from dataclasses import dataclass

from core.domain import Point

from .errors import CommandError
from .runtime import CommanderRuntime


@dataclass(frozen=True, slots=True)
class PointMetadata:
    """点表中声明的配置事实，不表示设备的实时控制许可。"""

    point_id: str
    access: str
    data_type: str
    unit: str
    scale: float
    offset: float
    configured_address: dict[str, str | int | float | bool]


@dataclass(frozen=True, slots=True)
class PointMetadataResult:
    """按请求顺序保留的单点查找结果。"""

    point_id: str
    metadata: PointMetadata | None


@dataclass(frozen=True, slots=True)
class PointsMetadataResult:
    """同一个激活 generation 的批量点表快照及配置标识。"""

    device_id: str
    active_revision: str
    active_config_hash: str
    points: tuple[PointMetadataResult, ...]


class CommanderMetadataService:
    """仅检查已激活点表；不建立协议连接或读取现场。"""

    def __init__(self, runtime: CommanderRuntime) -> None:
        self._runtime = runtime

    async def get_points_metadata(
        self,
        device_id: str,
        point_ids: list[str],
    ) -> PointsMetadataResult:
        """按请求顺序返回各点配置，未找到的点保留空 metadata。"""
        if not device_id:
            raise ValueError("device_id must not be empty")
        if not point_ids or any(not point_id for point_id in point_ids):
            raise ValueError("point_ids must contain non-empty values")

        async with self._runtime.operation():
            try:
                session = self._runtime.device(device_id)
            except KeyError as exc:
                raise CommandError(f"unknown device '{device_id}'") from exc
            revision, config_hash = self._runtime.operation_config_identity()
            points = session.point_table.points
            results: list[PointMetadataResult] = []
            for point_id in point_ids:
                point = points.get(point_id)
                metadata = _point_metadata(point) if point is not None else None
                results.append(PointMetadataResult(point_id=point_id, metadata=metadata))
            return PointsMetadataResult(
                device_id=device_id,
                active_revision=revision,
                active_config_hash=config_hash,
                points=tuple(results),
            )


def _point_metadata(point: Point) -> PointMetadata:
    """仅抽取公开的地址参数，不返回协议连接或认证配置。"""
    raw_type = point.ext.get("data_type", point.ext.get("type"))
    return PointMetadata(
        point_id=point.point_id,
        access=point.access.value,
        data_type=str(raw_type) if raw_type is not None else "",
        unit=point.source_unit.unit_id,
        scale=point.scale,
        offset=point.offset,
        configured_address={key: value for key, value in point.ext.items() if value is not None},
    )
