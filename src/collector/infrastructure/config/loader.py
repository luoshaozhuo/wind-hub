"""Collector 完整领域配置快照加载入口。"""

from __future__ import annotations

from dataclasses import replace

from core.application.config_snapshot import ConfigSnapshot
from core.application.errors import ConfigError
from core.application.settings import MessagingSettings, RuntimeSettings
from core.domain import Device, DeviceId
from core.infrastructure.config.collector_documents import (
    CollectorDocumentLoader,
    CollectorDocuments,
)
from core.infrastructure.config.collector_snapshot import build_collector_snapshot
from core.infrastructure.config.effective import resolve_effective_device

from ...application.config import CollectorConfig, RuntimeParams


def load_collector_config(source: CollectorDocumentLoader) -> CollectorConfig:
    """Load a new complete Collector effective configuration."""
    return collector_config_from_documents(source.load())


def collector_config_from_documents(documents: CollectorDocuments) -> CollectorConfig:
    """Construct an effective Collector config including persisted task intent."""
    snapshot = build_collector_snapshot(documents)
    return config_from_snapshot_and_documents(snapshot, documents)


def config_from_snapshot_and_documents(
    snapshot: ConfigSnapshot, documents: CollectorDocuments
) -> CollectorConfig:
    base = collector_config_from_snapshot(snapshot)
    return replace(
        base,
        desired_states={
            DeviceId(device_id): definition.desired_state
            for device_id, definition in documents.devices.items()
            if definition.enabled
        },
    )


def collector_config_from_snapshot(snapshot: ConfigSnapshot) -> CollectorConfig:
    devices = snapshot.site.devices
    settings = snapshot.system.runtime
    effective_devices: dict[DeviceId, Device] = {}
    for key, device in devices.items():
        if not device.enabled:
            continue
        model = snapshot.device_models[device.device_model_id]
        protocol = snapshot.point_tables[model.point_table_id].protocol.name
        merged_device, _ = resolve_effective_device(
            device,
            model,
            protocol,
        )
        effective_devices[key] = merged_device
    return CollectorConfig(
        units=snapshot.units,
        devices=effective_devices,
        device_models=snapshot.device_models,
        point_tables=snapshot.point_tables,
        runtime=_runtime_params(settings),
        ads_local=snapshot.system.ads,
        messaging=_required_messaging(snapshot),
        tasks=snapshot.tasks,
        disabled_devices=frozenset(
            DeviceId(key) for key, device in devices.items() if not device.enabled
        ),
    )


def _required_messaging(snapshot: ConfigSnapshot) -> MessagingSettings:
    """Require explicit NATS configuration for a Collector."""
    messaging = snapshot.system.messaging
    if messaging is None:
        raise ConfigError("system.messaging is required")
    return messaging


def _runtime_params(settings: RuntimeSettings) -> RuntimeParams:
    return RuntimeParams(
        shutdown_timeout=(
            settings.shutdown_timeout if settings.shutdown_timeout is not None else 10.0
        ),
        connect_timeout=settings.connect_timeout if settings.connect_timeout is not None else 1.0,
        read_timeout=settings.read_timeout if settings.read_timeout is not None else 1.0,
        write_timeout=settings.write_timeout if settings.write_timeout is not None else 1.0,
        watchdog_interval=settings.watchdog_interval,
        health_timeout=settings.health_timeout,
        thread_pool_max_workers=(
            settings.thread_pool.max_workers if settings.thread_pool is not None else 32
        ),
    )


__all__ = ["load_collector_config", "collector_config_from_snapshot"]
