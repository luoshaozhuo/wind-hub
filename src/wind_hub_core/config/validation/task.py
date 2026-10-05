"""采集 Task 跨文件组合约束校验。

Task 引用的 device / device_group / point_group / target sink 与命中设备
协议能力的组合合法性校验；输入全部是 resolved 配置模型。
"""

from __future__ import annotations

from wind_hub_core.config.model.device import DeviceConfig, DevicesConfig
from wind_hub_core.config.model.point import ResolvedPointTables
from wind_hub_core.config.model.task import CollectionTaskConfig
from wind_hub_core.model.errors import ConfigError


def validate_task_targets(
    task: CollectionTaskConfig,
    devices: DevicesConfig,
    point_tables: ResolvedPointTables,
    sink_names: set[str],
) -> None:
    """校验单个采集 Task 的跨文件引用。

    - ``device`` 必须存在；``device_group`` 至少匹配一台 enabled 设备；
    - 命中的 enabled 设备必须全部支持周期采集（ADS ``sequential`` 报错）；
    - ``point_group`` 必须在每台命中 enabled 设备的绑定点表中存在；
    - 每个 target sink 必须已定义。

    Raises:
        ConfigError: 任一引用缺失或组合非法。
    """
    for target in task.targets:
        if target.sink not in sink_names:
            raise ConfigError(
                f"Task '{task.task_id}' targets unknown sink "
                f"'{target.sink}' (available: {sorted(sink_names)})"
            )

    if task.device is not None:
        device = next((d for d in devices.devices if d.device_id == task.device), None)
        if device is None:
            raise ConfigError(f"Task '{task.task_id}' references unknown device '{task.device}'")
        matched = [device] if device.enabled else []
    else:
        matched = [d for d in devices.devices if d.enabled and d.device_group == task.device_group]
        if not any(d.device_group == task.device_group for d in devices.devices):
            raise ConfigError(
                f"Task '{task.task_id}': device_group '{task.device_group}' " "matches no device"
            )

    unsupported = [d.device_id for d in matched if not d.supports_scheduled_collection]
    if unsupported:
        raise ConfigError(
            f"Task '{task.task_id}': devices {unsupported} do not support "
            "scheduled collection (ADS read_mode='sequential' is single-read only)"
        )

    # interval 是否必填按命中设备的协议采集能力判定：主动轮询（Modbus、
    # ADS Sum）与 ADS 订阅（notification cycle_time）都需要节拍；纯
    # IEC104 订阅由远端决定数据到达时机，不要求 interval。混合命中时
    # （如 device_group 同时含 IEC104 与 Modbus）仍必须配置。
    if task.interval is None:
        requiring = [d.device_id for d in matched if _device_requires_interval(d)]
        if requiring:
            raise ConfigError(
                f"Task '{task.task_id}': interval is required — devices {requiring} "
                "collect actively (poll) or via ADS notification cycle_time; "
                "only pure IEC104-subscription tasks may omit interval"
            )

    missing = [
        d.device_id
        for d in matched
        if task.point_group
        not in {g for p in point_tables.tables[d.point_table].points for g in p.point_groups}
    ]
    if missing:
        raise ConfigError(
            f"Task '{task.task_id}': point_group '{task.point_group}' does not "
            f"exist in the point tables of devices {missing}"
        )


def _device_requires_interval(device: DeviceConfig) -> bool:
    """设备的采集机制是否需要 Task 提供节拍（interval）。

    - IEC104：订阅式（spontaneous / periodic），不需要；
    - ADS 且 ``subscribe_enabled``：订阅式，但 interval 用作 notification
      ``cycle_time``——需要；
    - 其余（Modbus、ADS Sum 主动轮询）：需要。
    """
    return device.protocol != "iec104"


__all__ = ["validate_task_targets"]
