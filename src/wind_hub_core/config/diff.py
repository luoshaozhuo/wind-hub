"""跨进程共享的配置差异计算。"""

from wind_hub_core.config.schema import Config
from wind_hub_core.model.reload import ConfigDiff, DeviceDiff, SinkDiff, TaskDiff


def compute_diff(old: Config, new: Config) -> ConfigDiff:
    """计算两个完整配置快照的结构化差异。"""
    old_devices = {item.device_id: item for item in old.devices.devices}
    new_devices = {item.device_id: item for item in new.devices.devices}
    old_ids, new_ids = set(old_devices), set(new_devices)
    devices = DeviceDiff(
        added=sorted(new_ids - old_ids),
        removed=sorted(old_ids - new_ids),
    )
    devices.updated = sorted(
        device_id
        for device_id in old_ids & new_ids
        if old_devices[device_id].model_dump() != new_devices[device_id].model_dump()
    )
    devices.unchanged = sorted((old_ids & new_ids) - set(devices.updated))

    old_sinks = {item.name: item for item in old.system.sinks}
    new_sinks = {item.name: item for item in new.system.sinks}
    old_sink_ids, new_sink_ids = set(old_sinks), set(new_sinks)
    sinks = SinkDiff(
        added=sorted(new_sink_ids - old_sink_ids),
        removed=sorted(old_sink_ids - new_sink_ids),
    )
    sinks.updated = sorted(
        name
        for name in old_sink_ids & new_sink_ids
        if old_sinks[name].model_dump() != new_sinks[name].model_dump()
    )
    sinks.unchanged = sorted((old_sink_ids & new_sink_ids) - set(sinks.updated))

    old_tasks = {item.task_id: item for item in old.tasks.tasks}
    new_tasks = {item.task_id: item for item in new.tasks.tasks}
    old_task_ids, new_task_ids = set(old_tasks), set(new_tasks)
    tasks = TaskDiff(
        added=sorted(new_task_ids - old_task_ids),
        removed=sorted(old_task_ids - new_task_ids),
    )
    tasks.updated = sorted(
        task_id
        for task_id in old_task_ids & new_task_ids
        if old_tasks[task_id].model_dump() != new_tasks[task_id].model_dump()
    )
    tasks.unchanged = sorted((old_task_ids & new_task_ids) - set(tasks.updated))

    old_tables = old.point_tables.tables
    new_tables = new.point_tables.tables
    table_names = set(old_tables) | set(new_tables)
    changed_tables = sorted(
        name
        for name in table_names
        if name not in old_tables
        or name not in new_tables
        or old_tables[name].model_dump() != new_tables[name].model_dump()
    )

    return ConfigDiff(
        devices=devices,
        sinks=sinks,
        tasks=tasks,
        points_changed=bool(changed_tables),
        point_tables_changed=changed_tables,
        units_changed=old.units != new.units,
    )
