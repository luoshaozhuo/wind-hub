"""从 Core 完整配置快照构造 Commander 运行配置。"""

from __future__ import annotations

from dataclasses import replace

from core.application.config_snapshot import ConfigSnapshot
from core.application.port import ConfigPort
from core.domain import ConnectionEndpoint, Device, DeviceId, ProtocolOptions

from ...application.config import CommanderConfig


def load_commander_config(source: ConfigPort) -> CommanderConfig:
    """从注入的配置来源加载 Commander 配置；不在 Loader 内实例化 Adapter。"""
    return _from_snapshot(source.load())


def _from_snapshot(snapshot: ConfigSnapshot) -> CommanderConfig:
    runtime = snapshot.system.runtime
    devices: dict[DeviceId, Device] = {}
    options: dict[DeviceId, ProtocolOptions] = {}
    for key, device in snapshot.site.devices.items():
        if not device.enabled:
            continue
        # 合并「设备显式 > 型号默认」得到有效 endpoint 与协议参数。
        model = snapshot.device_models[device.device_model_id]
        endpoint = device.endpoint
        if endpoint.port is None and model.endpoint_defaults.get("port") is not None:
            endpoint = ConnectionEndpoint(
                host=endpoint.host,
                port=int(model.endpoint_defaults["port"]),
            )
            device = replace(device, endpoint=endpoint)
        devices[key] = device
        options[key] = {
            **model.protocol_options,
            **snapshot.protocol_options_by_device.get(key, {}),
        }
    return CommanderConfig(
        devices=devices,
        device_models=snapshot.device_models,
        device_groups=snapshot.device_groups,
        point_tables=snapshot.point_tables,
        business_points=snapshot.business_points,
        protocol_options_by_device=options,
        ads_local=snapshot.system.ads,
        connect_timeout=runtime.connect_timeout if runtime.connect_timeout is not None else 1.0,
        write_timeout=runtime.write_timeout if runtime.write_timeout is not None else 1.0,
        read_timeout=runtime.read_timeout if runtime.read_timeout is not None else 1.0,
        watchdog_interval=runtime.watchdog_interval,
        health_timeout=runtime.health_timeout,
        disabled_devices=frozenset(
            key for key, device in snapshot.site.devices.items() if not device.enabled
        ),
    )


__all__ = ["load_commander_config"]
