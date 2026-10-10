"""配置快照序列化为七个 YAML 文档。"""

from __future__ import annotations

from typing import Any

from core.application.config_snapshot import ConfigSnapshot
from .codec import dump_system_config
from .point_table_writer import dump_point_tables


def dump_snapshot(snapshot: ConfigSnapshot) -> dict[str, dict[str, Any]]:
    system = dump_system_config(snapshot.system)
    system["site"] = {"site_id": snapshot.site.site_id, "name": snapshot.site.name}
    models = {}
    for key, model in snapshot.device_models.items():
        table = snapshot.point_tables[model.point_table_id]
        models[str(key)] = {
            "device_type": str(model.device_type_id),
            "model": model.name,
            "manufacturer": model.manufacturer,
            "protocol": table.protocol.name,
            "point_table": str(model.point_table_id),
        }
    devices = []
    for key, device in snapshot.site.devices.items():
        if len(device.device_group_ids) != 1:
            raise ValueError(f"device '{key}': multiple groups not yet supported by YAML")
        endpoint: dict[str, Any] = {"host": device.endpoint.host}
        if device.endpoint.port is not None:
            endpoint["port"] = device.endpoint.port
        options = dict(snapshot.protocol_options_by_device.get(key, {}))
        if options:
            endpoint["extensions"] = options
        devices.append({
            "device_id": str(key),
            "model": str(device.device_model_id),
            "device_group": str(device.device_group_ids[0]),
            "endpoint": endpoint,
            "enabled": True,
        })
