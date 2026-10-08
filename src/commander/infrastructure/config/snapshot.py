"""将 Core 共享配置解析结果适配为本进程的元数据类型。"""

from core.domain import CoreConfigSnapshot, DeviceId, PointTableId
from core.infrastructure.config.point_tables import ResolvedTable
from core.infrastructure.config.raw import DeviceInstancesFile, DeviceModelsFile, UnitsFile
from core.infrastructure.config.snapshot import build_core_snapshot as _build_core_snapshot

from ...application.config import PointMeta


def build_core_snapshot(
    *,
    models_file: DeviceModelsFile,
    instances_file: DeviceInstancesFile,
    tables: dict[str, ResolvedTable],
    units_file: UnitsFile,
) -> tuple[
    CoreConfigSnapshot,
    dict[PointTableId, dict[str, PointMeta]],
    frozenset[DeviceId],
]:
    snapshot, metadata, disabled, ads_subscribe = _build_core_snapshot(
        models_file=models_file,
        instances_file=instances_file,
        tables=tables,
        units_file=units_file,
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
    return snapshot, point_meta, disabled
