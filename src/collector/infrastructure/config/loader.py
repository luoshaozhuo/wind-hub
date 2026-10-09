"""Collector 现场配置加载入口。

通过 Core 类型化配置读取端口获取本进程所需主题（system / device /
points / units / tasks / sinks），完成 Core 快照组装、Sink 引用解析与
跨文件一致性校验，产出 CollectorConfig。

加载顺序与旧系统一致：read → parse → resolve → validate → build。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from core.application.config_types import SystemConfig
from core.application.port import ConfigReader
from core.domain import PointTableId
from core.infrastructure.config import YamlTypedConfigAdapter
from core.infrastructure.config.assembly import assemble_core_config

from ...application.config import (
    ADSLocalIdentity,
    BackpressurePolicy,
    CollectionTask,
    CollectorConfig,
    PointMeta,
    RuntimeParams,
)
from .sinks_resolver import resolve_sinks
from .tasks import validate_task_targets


def load_collector_config(
    config_dir: str | Path, *, reader: ConfigReader | None = None
) -> CollectorConfig:
    """加载 Collector 配置并完成跨文件一致性校验。

    Raises:
        ConfigError: 文件缺失、YAML 非法或任何配置约束违反。
    """
    reader = reader if reader is not None else YamlTypedConfigAdapter(config_dir)

    system = reader.read_system_config()
    device_config = reader.read_device_config()
    point_config = reader.read_point_tables_config()
    unit_config = reader.read_unit_config()
    task_config = reader.read_tasks_config()
    sinks_file = reader.read_sink_config()

    runtime = _runtime_params(system)
    ads_local = (
        ADSLocalIdentity(
            local_ams_net_id=system.ads.local_ams_net_id,
            local_ip=system.ads.local_ip,
        )
        if system.ads is not None
        else None
    )

    assembly = assemble_core_config(
        device_config=device_config,
        point_config=point_config,
        unit_config=unit_config,
    )
    point_meta = {
        table_id: {
            point_id: PointMeta(
                variable_name=meta.variable_name,
                point_groups=meta.point_groups,
            )
            for point_id, meta in points.items()
        }
        for table_id, points in assembly.point_meta.items()
    }

    # disabled 设备不进索引，但其点表仍可用于 Sink 引用解析（旧行为）。
    disabled_tables = {
        instance.device_id: assembly.point_tables[
            PointTableId(device_config.models.device_models[instance.model].point_table)
        ]
        for instance in device_config.instances.devices
        if not instance.enabled
    }
    sinks = resolve_sinks(sinks_file, assembly, unit_config, disabled_tables)

    tasks = {
        task.task_id: CollectionTask(
            task_id=task.task_id,
            device=task.device,
            device_group=task.device_group,
            point_group=task.point_group,
            interval=task.interval,
            targets=task.targets,
            enabled=task.enabled,
        )
        for task in task_config.tasks
    }

    sink_names = {name for name, sink in sinks.items() if sink.enabled}
    # device_group 匹配判定包含 disabled 设备的分组（与旧行为一致）。
    all_device_groups = {
        instance.device_group
        for instance in device_config.instances.devices
        if instance.device_group is not None
    }
    for task in tasks.values():
        validate_task_targets(
            task,
            assembly,
            point_meta,
            sink_names,
            all_device_groups,
            assembly.disabled_devices,
        )

    return CollectorConfig(
        devices=assembly.devices,
        device_models=assembly.device_models,
        point_tables=assembly.point_tables,
        device_options=assembly.device_options,
        runtime=runtime,
        ads_local=ads_local,
        tasks=tasks,
        sinks=sinks,
        point_meta=point_meta,
        ads_subscribe_devices=assembly.ads_subscribe_devices,
        disabled_devices=assembly.disabled_devices,
    )


def _runtime_params(system: SystemConfig) -> RuntimeParams:
    """把共享 RuntimeSettings 映射为 Collector 进程级运行时参数。

    缺省键使用 Collector 自身默认值；正值约束由 RuntimeParams 复核。
    """
    settings = system.runtime
    policy = settings.backpressure_policy or "drop_old"
    return RuntimeParams(
        queue_maxsize=settings.queue_maxsize if settings.queue_maxsize is not None else 1000,
        backpressure_policy=cast(BackpressurePolicy, policy),
        shutdown_timeout=(
            settings.shutdown_timeout if settings.shutdown_timeout is not None else 10.0
        ),
        connect_timeout=settings.connect_timeout if settings.connect_timeout is not None else 10.0,
        read_timeout=settings.read_timeout,
    )


__all__ = ["load_collector_config"]
