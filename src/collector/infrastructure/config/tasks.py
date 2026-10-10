"""采集 Task 跨文件组合约束校验（Collector 侧，基于 CoreConfigAssembly）。

Task 引用的 device / device_group / point_group / target sink 与命中设备
协议能力的组合合法性校验；输入全部是 resolved 配置模型。
"""

from __future__ import annotations

from collections.abc import Mapping

from collector.application.config import CollectionTask, PointMeta
from core.application import ConfigError
from core.domain import Device, DeviceId, PointTableId
from core.infrastructure.config.assembly import CoreConfigAssembly


def validate_task_targets(
    task: CollectionTask,
    assembly: CoreConfigAssembly,
    point_meta: Mapping[PointTableId, Mapping[str, PointMeta]],
    sink_names: set[str],
    all_device_groups: set[str],
    disabled_devices: frozenset[DeviceId],
) -> None:
    """校验单个采集 Task 的跨文件引用。

    - ``device`` 必须存在（含 disabled——disabled 设备不命中实例展开，
      但引用本身合法，与旧行为一致由 ``matched`` 为空体现）；
    - ``device_group`` 至少匹配一台设备（含 disabled，与旧行为一致）；
    - 命中的 enabled 设备必须全部支持周期采集（ADS ``sequential`` 报错）；
    - interval 是否必填按命中设备的协议采集能力判定；
    - ``point_group`` 必须在每台命中 enabled 设备的绑定点表中存在；
    - 每个 target sink 必须已定义且 enabled。

    Raises:
        ConfigError: 任一引用缺失或组合非法。
    """
    for target in task.targets:
        if target not in sink_names:
            raise ConfigError(
                f"Task '{task.task_id}' targets unknown sink "
                f"'{target}' (available: {sorted(sink_names)})"
            )

    if task.device is not None:
        device = assembly.devices.get(DeviceId(task.device))
        if device is None:
            # disabled 设备不进索引；引用合法但不命中任何实例（与旧行为一致）。
            if DeviceId(task.device) not in disabled_devices:
                raise ConfigError(
                    f"Task '{task.task_id}' references unknown device '{task.device}'"
                )
            matched: list[Device] = []
        else:
            matched = [device]
    else:
        matched = [
            d
            for d in assembly.devices.values()
            if any(str(g) == task.device_group for g in d.device_group_ids)
        ]
        if task.device_group not in all_device_groups:
            raise ConfigError(
                f"Task '{task.task_id}': device_group '{task.device_group}' " "matches no device"
            )

    unsupported = [str(d.device_id) for d in matched if not _supports_scheduled(assembly, d)]
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
        requiring = [str(d.device_id) for d in matched if _device_protocol(assembly, d) != "iec104"]
        if requiring:
            raise ConfigError(
                f"Task '{task.task_id}': interval is required — devices {requiring} "
                "collect actively (poll) or via ADS notification cycle_time; "
                "only pure IEC104-subscription tasks may omit interval"
            )

    missing = [
        str(d.device_id)
        for d in matched
        if task.point_group not in _point_groups(assembly, point_meta, d)
    ]
    if missing:
        raise ConfigError(
            f"Task '{task.task_id}': point_group '{task.point_group}' does not "
            f"exist in the point tables of devices {missing}"
        )


def _device_protocol(assembly: CoreConfigAssembly, device: Device) -> str:
    return assembly.point_table_for_device(device.device_id).protocol.name


def _supports_scheduled(assembly: CoreConfigAssembly, device: Device) -> bool:
    """ADS ``sequential`` 设备只允许请求式读取，不参与周期采集。"""
    if _device_protocol(assembly, device) != "ads":
        return True
    options = assembly.protocol_options_for(device.device_id)
    return options.get("read_mode") != "sequential"


def _point_groups(
    assembly: CoreConfigAssembly,
    point_meta: Mapping[PointTableId, Mapping[str, PointMeta]],
    device: Device,
) -> set[str]:
    table = assembly.point_table_for_device(device.device_id)
    meta = point_meta.get(table.point_table_id, {})
    return {
        group
        for point_id in table.points
        for group in meta.get(point_id, PointMeta(None, ())).point_groups
    }


__all__ = ["validate_task_targets"]
