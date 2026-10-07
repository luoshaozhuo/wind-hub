"""Collector 现场配置加载入口。

读取同一现场配置目录中的 Collector 子集（system / device_models /
devices / points / units / tasks / sinks），完成 Raw 解析、点表继承
展开、Core 快照组装、Sink 引用解析与跨文件一致性校验，产出
CollectorConfig。

加载顺序与旧系统一致：read → parse → resolve → validate → build。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

from core.application import ConfigError
from core.domain import PointTableId

from ...application.config import (
    BackpressurePolicy,
    CollectionTask,
    CollectorConfig,
    RuntimeParams,
)
from ...application.sinks import SinksConfig
from .point_tables import resolve_point_tables
from .raw import (
    DeviceInstancesFile,
    DeviceModelsFile,
    PointTablesFile,
    TasksFile,
    UnitsFile,
)
from .sinks_resolver import resolve_sinks
from .snapshot import build_core_snapshot
from .tasks import validate_task_targets

_BACKPRESSURE_POLICIES = ("drop_old", "drop_new", "block")


def load_collector_config(config_dir: str | Path) -> CollectorConfig:
    """加载 Collector 配置并完成跨文件一致性校验。

    Raises:
        ConfigError: 文件缺失、YAML 非法或任何配置约束违反。
    """
    base = Path(config_dir)

    system_raw = _read_yaml(base / "system.yaml")
    models_raw = _read_yaml(base / "device_models.yaml")
    devices_raw = _read_yaml(base / "devices.yaml")
    points_raw = _read_yaml(base / "points.yaml")
    units_raw = _read_yaml(base / "units.yaml")
    tasks_raw = _read_yaml(base / "tasks.yaml")
    sinks_raw = _read_yaml(base / "sinks.yaml")

    try:
        runtime = _parse_runtime_params(system_raw.get("runtime") or {})
        models_file = DeviceModelsFile(**models_raw)
        instances_file = DeviceInstancesFile(**devices_raw)
        tables_file = PointTablesFile(**points_raw)
        units_file = UnitsFile(**units_raw)
        tasks_file = TasksFile(**tasks_raw)
        sinks_file = SinksConfig(**sinks_raw)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Invalid collector configuration: {exc}") from exc

    tables = resolve_point_tables(tables_file.point_tables)
    snapshot, point_meta, disabled, ads_subscribe = build_core_snapshot(
        models_file=models_file,
        instances_file=instances_file,
        tables=tables,
        units_file=units_file,
    )

    # disabled 设备不进快照，但其点表仍可用于 Sink 引用解析（旧行为）。
    disabled_tables = {
        instance.device_id: snapshot.point_tables[
            PointTableId(models_file.device_models[instance.model].point_table)
        ]
        for instance in instances_file.devices
        if not instance.enabled
    }
    sinks = resolve_sinks(sinks_file, snapshot, units_file, disabled_tables)
    tasks = {
        raw.task_id: CollectionTask(
            task_id=raw.task_id,
            device=raw.device,
            device_group=raw.device_group,
            point_group=raw.point_group,
            interval=raw.interval,
            targets=tuple(target.sink for target in raw.targets),
            enabled=raw.enabled,
        )
        for raw in tasks_file.tasks
    }

    sink_names = {name for name, sink in sinks.items() if sink.enabled}
    # device_group 匹配判定包含 disabled 设备的分组（与旧行为一致）。
    all_device_groups = {
        instance.device_group
        for instance in instances_file.devices
        if instance.device_group is not None
    }
    for task in tasks.values():
        validate_task_targets(
            task,
            snapshot,
            point_meta,
            sink_names,
            all_device_groups,
            disabled,
        )

    return CollectorConfig(
        core=snapshot,
        runtime=runtime,
        tasks=tasks,
        sinks=sinks,
        point_meta=point_meta,
        ads_subscribe_devices=ads_subscribe,
        disabled_devices=disabled,
    )


def _parse_runtime_params(raw: Any) -> RuntimeParams:
    """解析 system.yaml ``runtime`` 段的 Collector 子集。"""
    if not isinstance(raw, dict):
        raise ConfigError("system.yaml 'runtime' must be a mapping")
    known = {
        "queue_maxsize",
        "backpressure_policy",
        "shutdown_timeout",
        "connect_timeout",
        "read_timeout",
        "write_timeout",  # Commander 子集，Collector 忽略
    }
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(f"system.yaml 'runtime' has unknown keys: {sorted(unknown)}")
    policy = raw.get("backpressure_policy", "drop_old")
    if policy not in _BACKPRESSURE_POLICIES:
        raise ConfigError(
            f"Invalid backpressure_policy '{policy}'; "
            f"must be one of {sorted(_BACKPRESSURE_POLICIES)}"
        )
    return RuntimeParams(
        queue_maxsize=int(raw.get("queue_maxsize", 1000)),
        backpressure_policy=cast(BackpressurePolicy, policy),
        shutdown_timeout=float(raw.get("shutdown_timeout", 10.0)),
        connect_timeout=float(raw.get("connect_timeout", 10.0)),
        read_timeout=(None if raw.get("read_timeout") is None else float(raw["read_timeout"])),
    )


def _read_yaml(path: Path) -> dict[str, Any]:
    """安全读取 YAML 根映射。"""
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            data = cast(dict[str, Any], yaml.safe_load(handle))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {path}: {exc}") from exc
    if data is None:
        raise ConfigError(f"Empty configuration file: {path}")
    if not isinstance(data, dict):
        raise ConfigError(f"Configuration root must be a mapping: {path}")
    return data


__all__ = ["load_collector_config"]
