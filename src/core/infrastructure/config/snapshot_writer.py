"""配置快照序列化为七个 YAML 文档。"""

from __future__ import annotations

from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from .point_table_writer import dump_point_tables


def dump_snapshot(
    snapshot: ConfigSnapshot,
    model_definitions: dict[str, Any] | None = None,
) -> dict[str, dict[str, Any]]:
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
    models = {}
    previous_models = (model_definitions or {}).get("device_models", {})
    for key, model in snapshot.device_models.items():
        table = snapshot.point_tables[model.point_table_id]
        previous = previous_models.get(str(key), {})
        models[str(key)] = {
            **previous,
            "device_type": str(model.device_type_id),
            "model": model.name,
            "manufacturer": model.manufacturer,
            "protocol": table.protocol.name,
            "point_table": str(model.point_table_id),
        }
    devices = []
    for key, device in snapshot.site.devices.items():
        endpoint: dict[str, Any] = {"host": device.endpoint.host}
        if device.endpoint.port is not None:
            endpoint["port"] = device.endpoint.port
        options = dict(snapshot.protocol_options_by_device.get(key, {}))
        if options:
            endpoint["extensions"] = options
        devices.append({
            "device_id": str(key),
            "model": str(device.device_model_id),
            "device_groups": [str(group_id) for group_id in device.device_group_ids],
            "endpoint": endpoint,
            "enabled": device.enabled,
        })
    tasks = [{
        "task_id": task.task_id,
        "device_group": str(task.device_group_id),
        "point_group": task.point_group,
        "interval": task.interval,
        "targets": [{"sink": sink_id} for sink_id in task.sink_ids],
        "enabled": task.enabled,
    } for task in snapshot.tasks.values()]
    catalog = {
        str(key): {
            "data_type": point.data_type.value,
            "unit": point.standard_unit.code.value,
            "description": point.description,
        }
        for key, point in snapshot.business_points.items()
    }
    return {
        "system.yaml": system,
        "device_models.yaml": {
            "device_types": {
                str(key): {"name": value.name}
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
            "sinks": [
                sink.model_dump(mode="json", by_alias=True)
                for sink in snapshot.sinks.values()
            ],
        },
    }
