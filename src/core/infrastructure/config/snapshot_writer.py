"""配置快照序列化为八个 YAML 文档。

与 :mod:`.parse_domain` 使用同一套 Schema：实体集合一律为 Mapping
（Key 即实体 ID），跨实体引用使用 ``*_id`` / ``*_ids`` 命名。
快照只保存显式配置（型号默认值在 DeviceModel 上，设备实例只写显式
endpoint/protocol_options），因此写回不依赖任何继承关系反推。
"""

from __future__ import annotations

from typing import Any

from core.application.config_snapshot import ConfigSnapshot

from .point_table_writer import dump_point_tables
from .units import dump_unit_catalog
from .yaml import yaml_plain as _yaml_plain


def dump_snapshot(snapshot: ConfigSnapshot) -> dict[str, dict[str, Any]]:
    system: dict[str, Any] = {}
    runtime = {
        name: getattr(snapshot.system.runtime, name)
        for name in snapshot.system.runtime.__dataclass_fields__
        if getattr(snapshot.system.runtime, name) is not None
    }
    if runtime:
        system["runtime"] = runtime
    if snapshot.system.ads is not None:
        system["ads"] = {
            name: getattr(snapshot.system.ads, name)
            for name in snapshot.system.ads.__dataclass_fields__
        }
    system["site"] = {"site_id": snapshot.site.site_id, "name": snapshot.site.name}

    models: dict[str, Any] = {}
    for model_key, model in snapshot.device_models.items():
        table = snapshot.point_tables[model.point_table_id]
        definition: dict[str, Any] = {
            "device_type_id": str(model.device_type_id),
            "model": model.name,
            "manufacturer": model.manufacturer,
            "protocol": table.protocol.name,
            "point_table_id": str(model.point_table_id),
            "properties": _yaml_plain(model.properties),
        }
        if model.endpoint_defaults:
            definition["endpoint_defaults"] = _yaml_plain(model.endpoint_defaults)
        if model.protocol_options:
            definition["protocol_options"] = _yaml_plain(model.protocol_options)
        models[str(model_key)] = definition

    devices: dict[str, Any] = {}
    for device_key, device in snapshot.site.devices.items():
        endpoint: dict[str, Any] = {"host": device.endpoint.host}
        if device.endpoint.port is not None:
            endpoint["port"] = device.endpoint.port
        options = dict(device.protocol_options)
        if device.subscribe_enabled:
            options["subscribe_enabled"] = True
        definition = {
            "model_id": str(device.device_model_id),
            **({"name": device.name} if device.name is not None else {}),
            "device_group_ids": [str(group_id) for group_id in device.device_group_ids],
            "endpoint": endpoint,
            **({"protocol_options": _yaml_plain(options)} if options else {}),
            "enabled": device.enabled,
        }
        devices[str(device_key)] = definition

    tasks = {
        task_id: {
            "device_group_id": str(task.device_group_id),
            "point_group": task.point_group,
            "interval": task.interval,
            "sink_ids": list(task.sink_ids),
            "enabled": task.enabled,
        }
        for task_id, task in snapshot.tasks.items()
    }
    catalog = {
        str(key): {
            "data_type": point.data_type.value,
            "unit": point.standard_unit.unit_id,
            "description": point.description,
        }
        for key, point in snapshot.business_points.items()
    }
    return {
        "system.yaml": system,
        "units.yaml": dump_unit_catalog(snapshot.units),
        "device_models.yaml": {
            "device_types": {
                str(key): {
                    "name": value.name,
                    **({"description": value.description} if value.description is not None else {}),
                }
                for key, value in snapshot.device_types.items()
            },
            "device_models": models,
        },
        "devices.yaml": {
            "devices": devices,
            "device_groups": {
                str(key): {"name": group.name, "description": group.description}
                for key, group in snapshot.device_groups.items()
            },
        },
        "points.yaml": dump_point_tables(snapshot.point_tables),
        "business_points.yaml": {"business_points": catalog},
        "tasks.yaml": {"tasks": tasks},
        "sinks.yaml": {
            "sinks": {
                sink_id: {
                    "type": sink.kind,
                    "enabled": sink.enabled,
                    "connection": _yaml_plain(sink.connection),
                    "points": _yaml_plain(sink.points),
                }
                for sink_id, sink in snapshot.sinks.items()
            },
        },
    }
