"""测试辅助——写出自包含的单目录配置集。

生产配置目录是完全独立、自包含的完整配置集（``system.yaml`` /
``device_models.yaml`` / ``points.yaml`` / ``devices.yaml`` /
``tasks.yaml`` / ``sinks.yaml`` / ``business_points.yaml`` /
``units.yaml``）。
:func:`write_config_tree` 把整套文件直接写进 ``base`` 并返回 ``base``
（即 ``load_config`` 的参数），与生产 loader 的目录规则一致。

全部实体集合使用统一 Schema 的 Mapping 形式（Key 即实体 ID）：

- ``devices``: ``{device_id: {model_id, endpoint, ...}}``；便捷写法允许
  条目带 ``protocol`` / ``point_table``——helper 自动派生型号并改写为
  ``model_id`` 引用（此时可省略 ``device_models``）。
- ``point_tables``: ``{table_id: {protocol, parent_id?, points: {pid: ...}}}``。
- ``sinks`` / ``tasks``: Mapping 形式。

点表 ``protocol``：基础表必填。测试传入的 Raw 表未写 ``protocol`` 时，
helper 按「绑定该表（或经 parent_id 与同组件表连通）的型号协议」自动
补全；无法推断时回落 ``"modbus"``。显式写出的 ``protocol`` 原样保留。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from tests.support.units import MINIMAL_UNITS_DOCUMENT

_ADS_LOGICAL_TYPES = {
    "BOOL": "bool",
    "SINT": "int8",
    "USINT": "uint8",
    "INT": "int16",
    "UINT": "uint16",
    "DINT": "int32",
    "UDINT": "uint32",
    "LINT": "int64",
    "ULINT": "uint64",
    "REAL": "float32",
    "LREAL": "float64",
    "STRING": "str",
}


def _write_yaml(dir_path: Path, name: str, data: dict[str, Any]) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / name
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return p


def _derive_models(
    devices: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """把带 protocol/point_table 的设备条目改写为 model_id 引用并派生型号。"""
    device_types: dict[str, Any] = {"turbine": {"name": "风机"}}
    device_models: dict[str, Any] = {}
    instances: dict[str, Any] = {}
    for device_id, entry in devices.items():
        d = dict(entry)
        if "protocol" not in d:
            instances[device_id] = d
            continue
        protocol = d.pop("protocol")
        point_table = d.pop("point_table")
        model_id = f"{protocol}__{point_table}"
        if model_id not in device_models:
            device_models[model_id] = {
                "device_type_id": "turbine",
                "protocol": protocol,
                "point_table_id": point_table,
            }
        instances[device_id] = {**d, "model_id": model_id}
    return instances, device_types, device_models


def _infer_table_protocols(
    point_tables: dict[str, Any],
    device_models: dict[str, Any],
) -> dict[str, str]:
    """推断每张 Raw 点表的 protocol（用于补全未显式写的表）。

    规则：型号直接绑定的表取型号协议；其余表沿 ``parent_id`` 边（双向）
    找同继承组件内已确定协议的表；都无法确定时回落 ``"modbus"``。
    """
    known: dict[str, str] = {}
    for m in device_models.values():
        table = m.get("point_table_id")
        if table in point_tables:
            known[table] = m["protocol"]

    # parent_id 无向连通组件
    parent = {name: name for name in point_tables}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        parent[_find(a)] = _find(b)

    for name, t in point_tables.items():
        ext = t.get("parent_id") if isinstance(t, dict) else None
        if ext in point_tables:
            _union(name, ext)

    comp_protocol: dict[str, str] = {}
    for name in point_tables:
        if name in known:
            comp_protocol[_find(name)] = known[name]

    return {
        name: known.get(name) or comp_protocol.get(_find(name), "modbus") for name in point_tables
    }


def write_config_tree(
    base: Path,
    *,
    devices: dict[str, Any],
    point_tables: dict[str, Any],
    device_models: dict[str, Any] | None = None,
    device_types: dict[str, Any] | None = None,
    device_groups: dict[str, Any] | None = None,
    sinks: dict[str, Any] | None = None,
    tasks: dict[str, Any] | None = None,
    system: dict[str, Any] | None = None,
) -> Path:
    """在 ``base`` 下写出自包含配置集，返回配置目录（即 ``base``）。"""
    base = Path(base)

    if device_models is None:
        instances, derived_types, derived_models = _derive_models(devices)
        device_types = device_types or derived_types
        device_models = derived_models
    else:
        instances = {device_id: dict(entry) for device_id, entry in devices.items()}
        device_types = device_types or {"turbine": {"name": "风机"}}

    tables = {name: dict(t) for name, t in point_tables.items()}
    inferred = _infer_table_protocols(tables, device_models)
    for name, t in tables.items():
        t.setdefault("protocol", inferred[name])

    # Business Point 身份即 Point Key（统一 Schema），据此汇总
    # business_points.yaml（当前 loader 的必需文件）。
    business_points: dict[str, dict[str, str]] = {}
    for table in tables.values():
        table_protocol = table.get("protocol")
        for point_id, point in table.get("points", {}).items():
            if "data_type" in point:
                data_type = point["data_type"]
                if table_protocol == "ads":
                    data_type = _ADS_LOGICAL_TYPES.get(str(data_type).upper(), data_type)
                definition = {
                    "data_type": data_type,
                    "unit": point.get("unit", "none"),
                }
                # 同一 Point Key 跨点表的类型冲突由 loader 检测，helper
                # 保留首个定义即可。
                business_points.setdefault(point_id, definition)

    # 未显式给出 device_groups 时，按设备引用的分组显式补全声明。
    declared = dict(device_groups or {})
    instances = {
        device_id: (
            {**entry, "device_group_ids": [device_id]} if "device_group_ids" not in entry else entry
        )
        for device_id, entry in instances.items()
    }
    for entry in instances.values():
        for group_id in entry["device_group_ids"]:
            declared.setdefault(group_id, {"name": str(group_id)})

    _write_yaml(base, "units.yaml", dict(MINIMAL_UNITS_DOCUMENT))
    _write_yaml(base, "business_points.yaml", {"business_points": business_points})
    _write_yaml(
        base,
        "device_models.yaml",
        {"device_types": device_types, "device_models": device_models},
    )
    _write_yaml(base, "points.yaml", {"point_tables": tables})

    system_data: dict[str, Any] = {"site": {"site_id": "test"}}
    if system:
        system_data.update(system)
    _write_yaml(base, "system.yaml", system_data)
    _write_yaml(
        base,
        "sinks.yaml",
        {
            "sinks": sinks
            if sinks is not None
            else {
                "s1": {
                    "type": "redis",
                    "connection": {"host": "127.0.0.1"},
                }
            }
        },
    )
    _write_yaml(
        base,
        "devices.yaml",
        {"devices": instances, "device_groups": declared},
    )
    _write_yaml(base, "tasks.yaml", {"tasks": tasks or {}})
    return base
