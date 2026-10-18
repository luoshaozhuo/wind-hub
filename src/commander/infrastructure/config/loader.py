"""从 Core 完整配置快照构造 Commander 运行配置。"""

from __future__ import annotations

from core.application.config_snapshot import ConfigSnapshot
from core.application.port import ConfigPort
from core.domain import Device, DeviceId
from core.infrastructure.config.effective import resolve_effective_device

from ...application.config import CommanderConfig


def load_commander_config(source: ConfigPort) -> CommanderConfig:
    """从注入的配置来源加载 Commander 配置；不在 Loader 内实例化 Adapter。"""
    return _from_snapshot(source.load())


def _from_snapshot(snapshot: ConfigSnapshot) -> CommanderConfig:
    runtime = snapshot.system.runtime
    devices: dict[DeviceId, Device] = {}
    for key, device in snapshot.site.devices.items():
        if not device.enabled:
            continue
        # 合并「设备显式 > 型号默认 > 协议代码默认」得到有效连接参数。
        model = snapshot.device_models[device.device_model_id]
        protocol = snapshot.point_tables[model.point_table_id].protocol.name
        merged_device, _ = resolve_effective_device(
            device,
            model,
            protocol,
        )
        devices[key] = merged_device
    return CommanderConfig(
        units=snapshot.units,
        devices=devices,
        device_models=snapshot.device_models,
        device_groups=snapshot.device_groups,
        point_tables=snapshot.point_tables,
        business_points=snapshot.business_points,
        ads_local=snapshot.system.ads,
        connect_timeout=runtime.connect_timeout if runtime.connect_timeout is not None else 1.0,
        write_timeout=runtime.write_timeout if runtime.write_timeout is not None else 1.0,
        read_timeout=runtime.read_timeout if runtime.read_timeout is not None else 1.0,
        watchdog_interval=runtime.watchdog_interval,
        health_timeout=runtime.health_timeout,
        thread_pool_max_workers=(
            runtime.thread_pool.max_workers if runtime.thread_pool is not None else 32
        ),
        disabled_devices=frozenset(
            key for key, device in snapshot.site.devices.items() if not device.enabled
        ),
    )


__all__ = ["load_commander_config"]
