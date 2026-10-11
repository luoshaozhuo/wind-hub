"""Collector 完整领域配置快照加载入口。"""

from __future__ import annotations

from pathlib import Path
from typing import cast

from core.application.config_snapshot import ConfigSnapshot
from core.application.port import ConfigPort
from core.application.settings import RuntimeSettings
from core.domain import DeviceId
from core.infrastructure.config import YamlConfigAdapter

from ...application.config import BackpressurePolicy, CollectorConfig, RuntimeParams


def load_collector_config(
    config_dir: str | Path, *, source: ConfigPort | None = None
) -> CollectorConfig:
    snapshot = (source if source is not None else YamlConfigAdapter(config_dir)).load()
    return _from_snapshot(snapshot)


def _from_snapshot(snapshot: ConfigSnapshot) -> CollectorConfig:
    devices = snapshot.site.devices
    settings = snapshot.system.runtime
    return CollectorConfig(
        devices={key: device for key, device in devices.items() if device.enabled},
        device_models=snapshot.device_models,
        point_tables=snapshot.point_tables,
        protocol_options_by_device=snapshot.protocol_options_by_device,
        runtime=_runtime_params(settings),
        ads_local=snapshot.system.ads,
        tasks=snapshot.tasks,
        sinks=snapshot.sinks,
        ads_subscribe_devices=snapshot.ads_subscribe_devices,
        disabled_devices=frozenset(
            DeviceId(key) for key, device in devices.items() if not device.enabled
        ),
    )


def _runtime_params(settings: RuntimeSettings) -> RuntimeParams:
    return RuntimeParams(
        queue_maxsize=settings.queue_maxsize if settings.queue_maxsize is not None else 1000,
        backpressure_policy=cast(BackpressurePolicy, settings.backpressure_policy or "drop_old"),
        shutdown_timeout=settings.shutdown_timeout if settings.shutdown_timeout is not None else 10.0,
        connect_timeout=settings.connect_timeout if settings.connect_timeout is not None else 1.0,
        read_timeout=settings.read_timeout if settings.read_timeout is not None else 1.0,
        write_timeout=settings.write_timeout if settings.write_timeout is not None else 1.0,
        watchdog_interval=settings.watchdog_interval,
        health_timeout=settings.health_timeout,
    )


__all__ = ["load_collector_config"]
