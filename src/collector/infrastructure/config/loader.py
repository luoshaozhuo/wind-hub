"""Build Collector runtime settings and tasks from the shared Core graph."""

from __future__ import annotations

from core.application.errors import ConfigError
from core.application.settings import ADSLocalConfig, MessagingSettings
from core.domain import DeviceGroupId, DeviceId, Task
from core.infrastructure.config import build_shared_snapshot

from ...application.config import CollectorConfig, RuntimeParams
from .documents import CollectorDocumentLoader, CollectorDocuments


def load_collector_config(source: CollectorDocumentLoader) -> CollectorConfig:
    """Read and validate an entire Collector configuration revision."""
    return collector_config_from_documents(source.load())


def collector_config_from_documents(documents: CollectorDocuments) -> CollectorConfig:
    """Resolve tasks and service settings against validated shared entities."""
    snapshot = build_shared_snapshot(documents.shared)
    service = documents.service
    runtime = service.get("runtime", {})
    watchdog = service.get("watchdog", {})
    messaging = service.get("messaging")
    if not isinstance(runtime, dict) or not isinstance(watchdog, dict):
        raise ConfigError("Collector runtime/watchdog must be mappings")
    if not isinstance(messaging, dict):
        raise ConfigError("Collector messaging must be configured")
    servers = messaging.get("servers")
    if not isinstance(servers, list) or not all(isinstance(x, str) for x in servers):
        raise ConfigError("Collector messaging.servers must be a list of NATS URLs")
    transport = MessagingSettings(
        servers=tuple(servers),
        delivery=messaging["delivery"],
        max_staleness_ms=messaging.get("max_staleness_ms"),
    )
    thread_pool = runtime.get("thread_pool", {})
    if not isinstance(thread_pool, dict):
        raise ConfigError("Collector runtime.thread_pool must be a mapping")
    params = RuntimeParams(
        shutdown_timeout=runtime.get("shutdown_timeout", 10.0),
        connect_timeout=runtime.get("connect_timeout", 1.0),
        read_timeout=runtime.get("read_timeout", 1.0),
        write_timeout=runtime.get("write_timeout", 1.0),
        watchdog_interval=watchdog.get("interval", 5.0),
        health_timeout=watchdog.get("health_timeout", 1.0),
        thread_pool_max_workers=thread_pool.get("max_workers", 32),
    )
    ads = service.get("ads")
    if ads is not None and not isinstance(ads, dict):
        raise ConfigError("Collector ads must be a mapping")
    ads_local = ADSLocalConfig(**ads) if ads is not None else None
    tasks: dict[str, Task] = {}
    desired: dict[DeviceId, str] = {}
    for identifier, definition in documents.devices.items():
        if not definition.enabled:
            continue
        task = definition.templates["task"].document
        task_override = definition.overrides.get("task", {})
        if not isinstance(task_override, dict):
            raise ConfigError(f"invalid task override for {identifier}")
        task_config = {**task, **task_override}
        tasks[identifier] = Task(
            task_id=identifier,
            device_group_id=DeviceGroupId(identifier),
            point_group=task_config["point_group"],
            interval=task_config["interval"],
        )
        desired[DeviceId(identifier)] = definition.desired_state
    devices = {key: value for key, value in snapshot.site.devices.items() if value.enabled}
    return CollectorConfig(
        units=snapshot.units,
        messaging=transport,
        devices=devices,
        device_models=snapshot.device_models,
        point_tables=snapshot.point_tables,
        runtime=params,
        ads_local=ads_local,
        tasks=tasks,
        desired_states=desired,
        disabled_devices=frozenset(
            DeviceId(identifier)
            for identifier, definition in documents.devices.items()
            if not definition.enabled
        ),
    )


__all__ = ["load_collector_config", "collector_config_from_documents"]
