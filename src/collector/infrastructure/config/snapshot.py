"""将 Core 共享配置解析结果适配为本进程的元数据类型。"""

from core.application.config_types import DeviceConfig, PointConfig, UnitConfig
from core.domain import CoreConfigSnapshot, DeviceId, PointTableId
from core.infrastructure.config.snapshot import build_core_snapshot as _build_core_snapshot

from ...application.config import PointMeta


def build_core_snapshot(
    *,
    device_config: DeviceConfig,
    point_config: PointConfig,
    unit_config: UnitConfig,
) -> tuple[
    CoreConfigSnapshot,
    dict[PointTableId, dict[str, PointMeta]],
    frozenset[DeviceId],
    frozenset[DeviceId],
]:
    snapshot, metadata, disabled, ads_subscribe = _build_core_snapshot(
        device_config=device_config,
        point_config=point_config,
        unit_config=unit_config,
    )
    point_meta = {
        table_id: {
            point_id: PointMeta(
                variable_name=meta.variable_name,
                point_groups=meta.point_groups,
            )
            for point_id, meta in points.items()
        }
        for table_id, points in metadata.items()
    }
    return snapshot, point_meta, disabled, ads_subscribe
