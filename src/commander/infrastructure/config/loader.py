"""从 Core 完整配置快照构造 Commander 运行配置。"""

from __future__ import annotations

from pathlib import Path

from core.application.config_snapshot import ConfigSnapshot
from core.application.port import ConfigPort
from core.infrastructure.config import YamlConfigAdapter

from ...application.config import CommanderConfig


def load_commander_config(
    config_dir: str | Path, *, source: ConfigPort | None = None
) -> CommanderConfig:
    snapshot = (source if source is not None else YamlConfigAdapter(config_dir)).load()
    return _from_snapshot(snapshot)


def _from_snapshot(snapshot: ConfigSnapshot) -> CommanderConfig:
    runtime = snapshot.system.runtime
    devices = snapshot.site.devices
    return CommanderConfig(
        devices={key: device for key, device in devices.items() if device.enabled},
        device_models=snapshot.device_models,
        device_groups=snapshot.device_groups,
        point_tables=snapshot.point_tables,
        business_points=snapshot.business_points,
        protocol_options_by_device=snapshot.protocol_options_by_device,
        ads_local=snapshot.system.ads,
        connect_timeout=runtime.connect_timeout if runtime.connect_timeout is not None else 1.0,
        write_timeout=runtime.write_timeout if runtime.write_timeout is not None else 1.0,
        read_timeout=runtime.read_timeout if runtime.read_timeout is not None else 1.0,
        watchdog_interval=runtime.watchdog_interval,
        health_timeout=runtime.health_timeout,
        disabled_devices=frozenset(
            key for key, device in devices.items() if not device.enabled
        ),
    )


__all__ = ["load_commander_config"]
