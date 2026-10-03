"""测试辅助——写出自包含的单目录配置集。

生产配置目录是完全独立、自包含的完整配置集（``system.yaml`` /
``units.yaml`` / ``device_models.yaml`` / ``points.yaml`` /
``devices.yaml`` / ``tasks.yaml`` / ``sinks.yaml``，``reporting.yaml`` 可选）。
:func:`write_config_tree` 把整套文件直接写进 ``base`` 并返回 ``base``
（即 ``load_config`` 的参数），与生产 loader 的目录规则一致。

``devices`` 接受两种写法：

- 实例式（新）：``{"device_id", "model", "endpoint", ...}``——原样写出，
  此时必须显式传 ``device_models``；
- 旧式便捷写法：带 ``protocol`` / ``point_table`` / ``read_mode`` 的 dict——
  helper 自动按 ``(protocol, point_table, read_mode)`` 派生型号并改写为
  实例式（端口留在实例 endpoint 上，``connection_defaults`` 为空）。

点表 ``protocol``：基础表必填。测试传入的 Raw 表未写 ``protocol`` 时，
helper 按「绑定该表（或经 extends 与同组件表连通）的型号协议」自动补全；
无法推断时回落 ``"modbus"``。显式写出的 ``protocol`` 原样保留。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

# 与 configs/template/units.yaml 保持一致的标准单位集（测试点引用的
# unit ID 默认都在其中）。
_DEFAULT_UNITS: dict[str, Any] = {
    "none": {"symbol": "", "name": "Dimensionless"},
    "percent": {"symbol": "%", "name": "Percent"},
    "volt": {"symbol": "V", "name": "Volt"},
    "kilovolt": {"symbol": "kV", "name": "Kilovolt"},
    "ampere": {"symbol": "A", "name": "Ampere"},
    "watt": {"symbol": "W", "name": "Watt"},
    "kilowatt": {"symbol": "kW", "name": "Kilowatt"},
    "megawatt": {"symbol": "MW", "name": "Megawatt"},
    "var": {"symbol": "var", "name": "Reactive power"},
    "kilovar": {"symbol": "kVar", "name": "Kilovar"},
    "megavar": {"symbol": "MVar", "name": "Megavar"},
    "hertz": {"symbol": "Hz", "name": "Hertz"},
    "rpm": {"symbol": "rpm", "name": "Revolutions per minute"},
    "meter_per_second": {"symbol": "m/s", "name": "Meter per second"},
    "degree": {"symbol": "deg", "name": "Degree"},
    "celsius": {"symbol": "°C", "name": "Degree Celsius"},
    "pascal": {"symbol": "Pa", "name": "Pascal"},
    "bar": {"symbol": "bar", "name": "Bar"},
    "second": {"symbol": "s", "name": "Second"},
    "millisecond": {"symbol": "ms", "name": "Millisecond"},
}


def _write_yaml(dir_path: Path, name: str, data: dict[str, Any]) -> Path:
    dir_path.mkdir(parents=True, exist_ok=True)
    p = dir_path / name
    with open(p, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f)
    return p


def _derive_models(
    devices: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """把旧式设备 dict 改写为实例式，并派生型号/类型定义。"""
    device_types: dict[str, Any] = {"turbine": {"name": "风机"}}
    device_models: dict[str, Any] = {}
    instances: list[dict[str, Any]] = []
    for d in devices:
        d = dict(d)
        protocol = d.pop("protocol")
        point_table = d.pop("point_table")
        read_mode = d.pop("read_mode", None)
        model_id = f"{protocol}__{point_table}" + (f"__{read_mode}" if read_mode else "")
        if model_id not in device_models:
            model: dict[str, Any] = {
                "device_type": "turbine",
                "protocol": protocol,
                "point_table": point_table,
            }
            if read_mode is not None:
                model["read_mode"] = read_mode
            device_models[model_id] = model
        instances.append({**d, "model": model_id})
    return instances, device_types, device_models


def _infer_table_protocols(
    point_tables: dict[str, Any],
    device_models: dict[str, Any],
) -> dict[str, str]:
    """推断每张 Raw 点表的 protocol（用于补全未显式写的表）。

    规则：型号直接绑定的表取型号协议；其余表沿 ``extends`` 边（双向）
    找同继承组件内已确定协议的表；都无法确定时回落 ``"modbus"``。
    """
    known: dict[str, str] = {}
    for m in device_models.values():
        table = m.get("point_table")
        if table in point_tables:
            known[table] = m["protocol"]

    # extends 无向连通组件
    parent = {name: name for name in point_tables}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        parent[_find(a)] = _find(b)

    for name, t in point_tables.items():
        ext = t.get("extends") if isinstance(t, dict) else None
        if ext in point_tables:
            _union(name, ext)

    comp_protocol: dict[str, str] = {}
    for name in point_tables:
        if name in known:
            comp_protocol[_find(name)] = known[name]

    return {
        name: known.get(name) or comp_protocol.get(_find(name), "modbus")
        for name in point_tables
    }


def write_config_tree(
    base: Path,
    *,
    devices: list[dict[str, Any]],
    point_tables: dict[str, Any],
    device_models: dict[str, Any] | None = None,
    device_types: dict[str, Any] | None = None,
    units: dict[str, Any] | None = None,
    sinks: list[dict[str, Any]] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    system: dict[str, Any] | None = None,
    reporting: dict[str, Any] | None = None,
) -> Path:
    """在 ``base`` 下写出自包含配置集，返回配置目录（即 ``base``）。"""
    base = Path(base)

    if device_models is None:
        instances, derived_types, derived_models = _derive_models(devices)
        device_types = device_types or derived_types
        device_models = derived_models
    else:
        instances = devices
        device_types = device_types or {"turbine": {"name": "风机"}}

    tables = {name: dict(t) for name, t in point_tables.items()}
    inferred = _infer_table_protocols(tables, device_models)
    for name, t in tables.items():
        t.setdefault("protocol", inferred[name])

    _write_yaml(base, "units.yaml", {"units": units or dict(_DEFAULT_UNITS)})
    _write_yaml(
        base,
        "device_models.yaml",
        {"device_types": device_types, "device_models": device_models},
    )
    _write_yaml(base, "points.yaml", {"point_tables": tables})

    system_data: dict[str, Any] = {}
    if system:
        system_data.update(system)
    _write_yaml(base, "system.yaml", system_data)
    _write_yaml(
        base,
        "sinks.yaml",
        {
            "sinks": sinks
            if sinks is not None
            else [
                {
                    "name": "s1",
                    "type": "file",
                    "connection": {"path": "/tmp/wind-hub-test.jsonl"},
                }
            ]
        },
    )
    _write_yaml(base, "devices.yaml", {"devices": instances})
    _write_yaml(base, "tasks.yaml", {"tasks": tasks or []})
    if reporting is not None:
        _write_yaml(base, "reporting.yaml", reporting)
    return base
