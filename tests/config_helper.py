"""测试辅助——写出两级配置目录（``common/`` + ``site/``）。

生产配置组织为「公共产品定义 + 单现场实例配置」：

- ``common/device_models.yaml`` — 设备类型 + 设备型号（点表绑定在型号层）；
- ``common/points.yaml`` — 命名点表；
- ``site/system.yaml`` / ``devices.yaml`` / ``tasks.yaml``（``reporting.yaml``
  可选）——现场实例配置。

:func:`write_config_tree` 在 ``base`` 下生成 ``base/common`` 与
``base/site`` 并返回 **site 目录**（``load_config`` 的参数；公共目录按
``site.parent / 'common'`` 默认解析，与生产一致）。

``devices`` 接受两种写法：

- 实例式（新）：``{"device_id", "model", "endpoint", ...}``——原样写出，
  此时必须显式传 ``device_models``；
- 旧式便捷写法：带 ``protocol`` / ``point_table`` / ``read_mode`` 的 dict——
  helper 自动按 ``(protocol, point_table, read_mode)`` 派生型号并改写为
  实例式（端口留在实例 endpoint 上，``connection_defaults`` 为空）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


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


def write_config_tree(
    base: Path,
    *,
    devices: list[dict[str, Any]],
    point_tables: dict[str, Any],
    device_models: dict[str, Any] | None = None,
    device_types: dict[str, Any] | None = None,
    sinks: list[dict[str, Any]] | None = None,
    tasks: list[dict[str, Any]] | None = None,
    system: dict[str, Any] | None = None,
    reporting: dict[str, Any] | None = None,
) -> Path:
    """在 ``base`` 下写出完整配置树，返回 site 配置目录。"""
    base = Path(base)
    common = base / "common"
    site = base / "site"

    if device_models is None:
        instances, derived_types, derived_models = _derive_models(devices)
        device_types = device_types or derived_types
        device_models = derived_models
    else:
        instances = devices
        device_types = device_types or {"turbine": {"name": "风机"}}

    _write_yaml(
        common,
        "device_models.yaml",
        {"device_types": device_types, "device_models": device_models},
    )
    _write_yaml(common, "points.yaml", {"point_tables": point_tables})

    system_data: dict[str, Any] = {
        "sinks": sinks if sinks is not None else [{"name": "s1", "type": "file"}]
    }
    if system:
        system_data.update(system)
    _write_yaml(site, "system.yaml", system_data)
    _write_yaml(site, "devices.yaml", {"devices": instances})
    _write_yaml(site, "tasks.yaml", {"tasks": tasks or []})
    if reporting is not None:
        _write_yaml(site, "reporting.yaml", reporting)
    return site
