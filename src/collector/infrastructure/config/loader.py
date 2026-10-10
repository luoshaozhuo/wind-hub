"""Collector 现场配置加载入口。

通过 Core 统一配置服务获取本进程所需主题（system / device_models /
devices / points / units / tasks / sinks）的配置 VO，完成 Core 快照
组装、Sink 引用解析与跨文件一致性校验，产出 CollectorConfig。

加载顺序与旧系统一致：read → parse → resolve → validate → build。
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

from core.application import ConfigService
from core.application.port import ConfigSnapshot, ConfigTopic, ConfigValue
from core.application.sink_config import SinksConfig
from core.domain import PointTableId
from core.domain.config import (
    DeviceModelsConfig,
    DevicesConfig,
    PointTablesConfig,
    SystemConfig,
    TasksConfig,
    UnitsConfig,
)
from core.infrastructure.config import YamlConfigAdapter
from core.infrastructure.config.assembly import assemble_core_config

from ...application.config import (
    ADSLocalIdentity,
    BackpressurePolicy,
    CollectorConfig,
    PointMeta,
    RuntimeParams,
)
from .sinks_resolver import resolve_sinks
from .tasks import validate_task_targets

COLLECTOR_CONFIG_TOPICS: tuple[ConfigTopic, ...] = tuple(ConfigTopic)


class _ConfigSource(Protocol):
    """配置来源：ConfigPort 或一致性快照（均有 ``read(topic)``）。"""

    def read(self, topic: ConfigTopic) -> ConfigValue: ...


def load_collector_config(
    config_dir: str | Path, *, source: _ConfigSource | None = None
) -> CollectorConfig:
    """加载 Collector 配置并完成跨文件一致性校验。

    默认在一致性快照内读取 Collector 全部主题：会话内不混用不同
    版本，加载期间的外部并发修改经 ``verify_unchanged`` 检测并中止。

    Raises:
        ConfigError: 文件缺失、YAML 非法、加载期间配置被修改或任何
            配置约束违反。
    """
    if source is not None:
        return _load_from(source)
    adapter = YamlConfigAdapter(config_dir)
    service = ConfigService(adapter, snapshots=adapter)
    snapshot = service.open_snapshot(COLLECTOR_CONFIG_TOPICS)
    config = _load_from(snapshot)
    snapshot.verify_unchanged()
    return config


def _load_from(source: _ConfigSource | ConfigSnapshot) -> CollectorConfig:
    system = cast(SystemConfig, source.read(ConfigTopic.SYSTEM))
    models = cast(DeviceModelsConfig, source.read(ConfigTopic.DEVICE_MODELS))
    devices = cast(DevicesConfig, source.read(ConfigTopic.DEVICES))
    points = cast(PointTablesConfig, source.read(ConfigTopic.POINTS))
    units = cast(UnitsConfig, source.read(ConfigTopic.UNITS))
    tasks = cast(TasksConfig, source.read(ConfigTopic.TASKS))
    sinks = cast(SinksConfig, source.read(ConfigTopic.SINKS))

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
        device_models_config=models,
        devices_config=devices,
        point_config=points,
        unit_config=units,
    )
    point_meta = {
        table_id: {
            point_id: PointMeta(
                variable_name=meta.variable_name,
                point_groups=meta.point_groups,
            )
            for point_id, meta in table_points.items()
        }
        for table_id, table_points in assembly.point_meta.items()
    }

    # disabled 设备不进索引，但其点表仍可用于 Sink 引用解析（旧行为）。
    disabled_tables = {
        instance.device_id: assembly.point_tables[
            PointTableId(models.device_models[instance.model].point_table)
        ]
        for instance in devices.devices
        if not instance.enabled
    }
    resolved_sinks = resolve_sinks(sinks, assembly, units, disabled_tables)

    # device_group 匹配判定包含 disabled 设备的分组（与旧行为一致）。
    all_device_groups = {
        instance.device_group for instance in devices.devices if instance.device_group is not None
    }
    sink_names = {name for name, sink in resolved_sinks.items() if sink.enabled}
    task_map = {task.task_id: task for task in tasks.tasks}
    for task in task_map.values():
        validate_task_targets(
            task,
            assembly,
            point_meta,
            sink_names,
            all_device_groups,
            assembly.disabled_devices,
        )

    return CollectorConfig(
        configs={
            ConfigTopic.SYSTEM: system,
            ConfigTopic.DEVICE_MODELS: models,
            ConfigTopic.DEVICES: devices,
            ConfigTopic.POINTS: points,
            ConfigTopic.UNITS: units,
            ConfigTopic.TASKS: tasks,
            ConfigTopic.SINKS: sinks,
        },
        devices=assembly.devices,
        device_models=assembly.device_models,
        point_tables=assembly.point_tables,
        protocol_options_by_device=assembly.protocol_options_by_device,
        runtime=runtime,
        ads_local=ads_local,
        tasks=task_map,
        sinks=resolved_sinks,
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
        connect_timeout=settings.connect_timeout if settings.connect_timeout is not None else 1.0,
        read_timeout=(settings.read_timeout if settings.read_timeout is not None else 1.0),
        write_timeout=settings.write_timeout if settings.write_timeout is not None else 1.0,
        reconnect_attempts=settings.reconnect_attempts,
    )


__all__ = ["load_collector_config"]
