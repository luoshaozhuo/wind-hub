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

from core.application import ConfigError
from core.application.port.config import CollectorConfigReader
from core.domain import PointTableId
from core.infrastructure.config import YamlTypedConfigAdapter

from ...application.config import (
    ADSLocalIdentity,
    BackpressurePolicy,
    CollectionTask,
    CollectorConfig,
    RuntimeParams,
)
from ...application.sinks import SinksConfig
from .point_tables import resolve_point_tables
from .raw import (
    ADSSystemRaw,
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


def load_collector_config(
    config_dir: str | Path, *, reader: CollectorConfigReader | None = None
) -> CollectorConfig:
    """加载 Collector 配置并完成跨文件一致性校验。

    Raises:
        ConfigError: 文件缺失、YAML 非法或任何配置约束违反。
    """
    reader = reader if reader is not None else YamlTypedConfigAdapter(config_dir)

    system_raw = reader.read_system()
    # 兼容已注入的旧 Raw Reader；默认路径统一使用 Core 类型化 Adapter。
    typed = isinstance(reader, YamlTypedConfigAdapter)
    if typed:
        device_config = reader.read_device_config()
        point_config = reader.read_point_config()
        unit_config = reader.read_unit_config()
        task_config = reader.read_task_config()
    else:
        models_raw = reader.read_device_models()
        devices_raw = reader.read_devices()
        points_raw = reader.read_points()
        units_raw = reader.read_units()
        tasks_raw = reader.read_tasks()
    sinks_raw = reader.read_sinks()

    try:
        runtime = _parse_runtime_params(system_raw.get("runtime") or {})
        ads_local = _parse_ads_local_identity(system_raw.get("ads"))
        models_file = device_config.models if typed else DeviceModelsFile(**models_raw)
        instances_file = device_config.instances if typed else DeviceInstancesFile(**devices_raw)
        tables_file = None if typed else PointTablesFile(**points_raw)
        units_file = unit_config.definition if typed else UnitsFile(**units_raw)
        tasks_file = task_config.definition if typed else TasksFile(**tasks_raw)
        sinks_file = SinksConfig(**sinks_raw)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(f"Invalid collector configuration: {exc}") from exc

    tables = dict(point_config.tables) if typed else resolve_point_tables(tables_file.point_tables)
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
        ads_local=ads_local,
        tasks=tasks,
        sinks=sinks,
        point_meta=point_meta,
        ads_subscribe_devices=ads_subscribe,
        disabled_devices=disabled,
    )


def _parse_ads_local_identity(raw: Any) -> ADSLocalIdentity | None:
    """解析 system.yaml ``ads`` 段的进程级本机身份（restart-required）。"""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("system.yaml 'ads' must be a mapping")
    parsed = ADSSystemRaw(**raw)
    return ADSLocalIdentity(
        local_ams_net_id=parsed.local_ams_net_id,
        local_ip=parsed.local_ip,
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


__all__ = ["load_collector_config"]
