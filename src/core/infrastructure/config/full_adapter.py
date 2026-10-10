"""全量 YAML 读取入口：一次构造完整领域快照。"""

from __future__ import annotations

from pathlib import Path

from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError
from core.domain import Task, validate_core_config

from .assembly import assemble_core_config
from .business_points import parse_business_points
from .codec import (
    parse_device_models_config,
    parse_devices_config,
    parse_point_tables_config,
    parse_sinks_config,
    parse_system_config,
    parse_tasks_config,
)
from .yaml import read_yaml_mapping


class FullYamlConfigAdapter:
    """读取整个现场配置目录，不向调用方暴露按主题加载。"""

    def __init__(self, config_dir: str | Path) -> None:
        self._base = Path(config_dir)

    def load(self) -> ConfigSnapshot:
        """读取、构建并统一进行领域关系校验。"""
        base = self._base
        system = parse_system_config(read_yaml_mapping(base / "system.yaml"))
        models = parse_device_models_config(read_yaml_mapping(base / "device_models.yaml"))
        devices = parse_devices_config(read_yaml_mapping(base / "devices.yaml"))
        tables = parse_point_tables_config(read_yaml_mapping(base / "points.yaml"))
        tasks = parse_tasks_config(read_yaml_mapping(base / "tasks.yaml"))
        sinks = parse_sinks_config(read_yaml_mapping(base / "sinks.yaml"))

        business_points = parse_business_points(
            read_yaml_mapping(base / "business_points.yaml")
        )

        assembly = assemble_core_config(
            defined_business_points=business_points,
            device_models_config=models,
            devices_config=devices,
            point_config=tables,
        )

        domain_tasks: dict[str, Task] = {}
        try:
            for task in tasks.tasks.values():
                if task.device_group is None:
                    raise ValueError(
                        f"task '{task.task_id}' must specify device_group, not device"
                    )
                if task.device is not None:
                    raise ValueError(f"task '{task.task_id}' cannot specify device")
                if task.interval is None:
                    raise ValueError(f"task '{task.task_id}' must specify interval")
                domain_tasks[task.task_id] = Task(
                    task_id=task.task_id,
                    device_group_id=task.device_group,
                    point_group=task.point_group,
                    sink_ids=task.targets,
                    interval=task.interval,
                    enabled=task.enabled,
                )

            sink_map = {sink.name: sink for sink in sinks.sinks}
            validate_core_config(
                device_types=assembly.device_types,
                device_models=assembly.device_models,
                device_groups=assembly.device_groups,
                devices=assembly.devices,
                business_points=assembly.business_points,
                point_tables=assembly.point_tables,
                protocol_options_by_device=assembly.protocol_options_by_device,
                tasks=domain_tasks,
                sink_ids=set(sink_map),
            )
        except ValueError as exc:
            raise ConfigError(f"Invalid domain configuration: {exc}") from exc

        return ConfigSnapshot(
            system=system,
            device_types=assembly.device_types,
            device_models=assembly.device_models,
            device_groups=assembly.device_groups,
            devices=assembly.devices,
            point_tables=assembly.point_tables,
            business_points=assembly.business_points,
            tasks=domain_tasks,
            sinks=sink_map,
            protocol_options_by_device=assembly.protocol_options_by_device,
            point_meta=assembly.point_meta,
        )
