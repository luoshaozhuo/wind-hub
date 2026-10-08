"""类型化配置到旧快照构造输入的过渡转换。

兼容层只位于 Infrastructure；Core Application 的契约不依赖 Raw Schema。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core.application.config_types import DeviceConfig, PointConfig
from core.infrastructure.config.point_tables import ResolvedTable
from core.infrastructure.config.raw import (
    DeviceInstancesFile,
    DeviceModelsFile,
    PointConfigRaw,
)


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def to_raw_devices(config: DeviceConfig) -> tuple[DeviceModelsFile, DeviceInstancesFile]:
    """把已验证的不可变设备配置转换为旧快照构造器的输入。"""
    models = DeviceModelsFile.model_validate({
        "device_types": {
            key: {"name": item.name}
            for key, item in config.models.device_types.items()
        },
        "device_models": {
            key: {
                "device_type": item.device_type,
                "manufacturer": item.manufacturer,
                "model": item.model,
                "protocol": item.protocol,
                "point_table": item.point_table,
                "read_mode": item.read_mode,
                "properties": _plain(item.properties),
                "connection_defaults": _plain(item.connection_defaults),
            }
            for key, item in config.models.device_models.items()
        },
    })
    instances = DeviceInstancesFile.model_validate({
        "devices": [
            {
                "device_id": item.device_id,
                "model": item.model,
                "device_group": item.device_group,
                "endpoint": {
                    "host": item.endpoint.host,
                    "port": item.endpoint.port,
                    "extensions": _plain(item.endpoint.extensions),
                },
                "enabled": item.enabled,
            }
            for item in config.instances.devices
        ],
    })
    return models, instances


def to_raw_point_tables(config: PointConfig) -> dict[str, ResolvedTable]:
    """转换已完成继承展开的点表，保持点声明顺序。"""
    return {
        table_name: ResolvedTable(
            protocol=table.protocol,
            points={
                point_id: PointConfigRaw.model_validate({
                    "point_id": point.point_id,
                    "variable_name": point.variable_name,
                    "point_groups": list(point.point_groups),
                    "address": _plain(point.address),
                    "data_type": point.data_type,
                    "scale": point.scale,
                    "offset": point.offset,
                    "unit": point.unit,
                    "description": point.description,
                })
                for point_id, point in table.points.items()
            },
        )
        for table_name, table in config.tables.items()
    }


__all__ = ["to_raw_devices", "to_raw_point_tables"]
