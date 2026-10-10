"""全量 YAML 读取入口：一次构造完整领域快照。"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from shutil import copy2

from core.application.config_snapshot import ConfigSnapshot
from core.application.config_diff import diff_config_snapshots
from core.application.errors import ConfigError
from core.domain import Site, Task, validate_core_config

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
from .point_table_writer import dump_point_tables
from .snapshot_writer import dump_snapshot
from .yaml import read_yaml_mapping, write_yaml_mapping_atomic


class YamlConfigAdapter:
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

        if not system.site_id:
            raise ConfigError("system.yaml site.site_id is required")
        site = Site(
            site_id=system.site_id,
            name=system.site_name or system.site_id,
            devices=assembly.devices,
        )
        return ConfigSnapshot(
            system=system,
            site=site,
            device_types=assembly.device_types,
            device_models=assembly.device_models,
            device_groups=assembly.device_groups,
            point_tables=assembly.point_tables,
            business_points=assembly.business_points,
            tasks=domain_tasks,
            sinks=sink_map,
            protocol_options_by_device=assembly.protocol_options_by_device,
        )

    def save(self, snapshot: ConfigSnapshot) -> None:
        """先在临时目录完整验证，避免将不等价快照写回配置目录。"""
        documents = dump_snapshot(
            snapshot,
            read_yaml_mapping(self._base / "device_models.yaml"),
        )
        original = read_yaml_mapping(self._base / 'system.yaml')
        documents['system.yaml'] = {**original, **documents['system.yaml']}
        with TemporaryDirectory(prefix="wind-hub-config-") as directory:
            staging = Path(directory)
            for path in self._base.glob("*.yaml"):
                copy2(path, staging / path.name)
            for filename, data in documents.items():
                write_yaml_mapping_atomic(staging / filename, data)
            restored = YamlConfigAdapter(staging).load()
            difference = diff_config_snapshots(snapshot, restored)
            if difference.has_changes:
                raise ConfigError(
                    "Cannot save configuration losslessly: "
                    f"changed sections={tuple(name for name, part in difference.sections.items() if part.has_changes)}, "
                    f"site_changed={difference.site_changed}, system_changed={difference.system_changed}"
                )
            # Keep a recoverable copy of every overwritten file.
            with TemporaryDirectory(prefix="wind-hub-backup-") as backup_dir:
                backup = Path(backup_dir)
                for filename in documents:
                    copy2(self._base / filename, backup / filename)
                try:
                    for filename, data in documents.items():
                        write_yaml_mapping_atomic(self._base / filename, data)
                except BaseException:
                    # Restore all originals, including files overwritten before failure.
                    for filename in documents:
                        copy2(backup / filename, self._base / filename)
                    raise

    def save_point_tables(self, snapshot: ConfigSnapshot) -> None:
        """根据完整父子 PointTable 差异保存 points.yaml。"""
        write_yaml_mapping_atomic(
            self._base / "points.yaml",
            dump_point_tables(snapshot.point_tables),
        )
