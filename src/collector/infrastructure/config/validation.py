"""Core-backed validation for Collector's full configuration snapshot."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

from collector.application.acquisition import AcquisitionPlan, DevicePlan
from collector.application.config_models import SnapshotPaths
from collector.application.lifecycle import ConfiguredRuntime
from collector.application.ports.task_intents import TaskIntentPort
from contracts.sample_batch import validate_subject_token
from core.application.errors import ConfigError
from core.configuration import SharedConfigPaths, SharedDocumentLoader, build_shared_snapshot
from core.configuration.document_version import validate_document_version
from core.configuration.effective import resolve_effective_device
from core.configuration.template_graph import (
    resolve_device_templates,
    validate_point_table_chain,
)
from core.configuration.versioned_templates import (
    TEMPLATE_FILE_PREFIXES,
    TemplateRef,
    VersionedTemplateRepository,
)
from core.domain import DeviceId
from core.infrastructure.config import read_yaml


def resolve_source_paths(config_file: Path) -> SnapshotPaths:
    """Resolve exactly the Instances selected by the active Deployment."""
    from core.configuration.local_selection import select_local_service

    try:
        selected = select_local_service(config_file)
        site_path = selected.instances["site"]
        devices_path = selected.instances["devices"]
        tasks_path = selected.instances["tasks"]
    except (KeyError, ValueError, OSError) as exc:
        raise ConfigError(f"Collector Deployment selection failed: {exc}") from exc
    return SnapshotPaths(
        service=selected.service_path,
        instances=selected.service_path.parent.parent,
        templates=selected.templates,
        site=site_path,
        devices=devices_path,
        tasks=tasks_path,
    )


def _source_references(service: Mapping[str, object]) -> tuple[str, ...]:
    """Return validated Collector Instance logical identities."""
    from core.configuration.deployment import INSTANCE_ROLE_TYPES, service_instance_refs

    try:
        refs = service_instance_refs(service, INSTANCE_ROLE_TYPES)
    except ValueError as exc:
        raise ConfigError(f"invalid Collector Instance identity: {exc}") from exc
    sources = service.get("config_sources")
    if not isinstance(sources, dict):
        raise ConfigError("Collector config_sources must be a mapping")
    instances = sources.get("instances")
    if not isinstance(instances, dict) or set(instances) != {"site", "devices", "tasks"}:
        raise ConfigError("Collector requires site, devices and tasks Instance roles")
    return tuple(config_id for _, config_id in refs)


def _positive(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{field} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ConfigError(f"{field} must be positive and finite")
    return number


def _section(document: Mapping[str, object], name: str) -> dict[str, object]:
    value = document.get(name, {})
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigError(f"{name} must be a string-keyed mapping")
    return value


def _reject_unknown(values: Mapping[str, object], allowed: set[str], field: str) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise ConfigError(f"{field} has unknown fields: {sorted(unknown)}")


def validate_service_settings(document: Mapping[str, object]) -> None:
    """Validate Collector-owned service parameters before activation."""
    runtime = _section(document, "runtime")
    _reject_unknown(
        runtime,
        {"shutdown_timeout", "connect_timeout", "read_timeout", "write_timeout", "thread_pool"},
        "runtime",
    )
    for name in ("shutdown_timeout", "connect_timeout", "read_timeout", "write_timeout"):
        if name in runtime:
            _positive(runtime[name], f"runtime.{name}")
    pool = runtime.get("thread_pool", {})
    if not isinstance(pool, dict):
        raise ConfigError("runtime.thread_pool must be a mapping")
    _reject_unknown(pool, {"max_workers"}, "runtime.thread_pool")
    if "max_workers" in pool:
        value = pool["max_workers"]
        if type(value) is not int or value <= 0:
            raise ConfigError("runtime.thread_pool.max_workers must be a positive integer")
    watchdog = _section(document, "watchdog")
    _reject_unknown(watchdog, {"interval", "health_timeout"}, "watchdog")
    for name in ("interval", "health_timeout"):
        if name in watchdog:
            _positive(watchdog[name], f"watchdog.{name}")
    messaging = _section(document, "messaging")
    _reject_unknown(
        messaging,
        {
            "servers",
            "delivery",
            "max_staleness_ms",
            "max_samples_per_batch",
            "max_batch_bytes",
            "queue_capacity",
            "enqueue_timeout_s",
        },
        "messaging",
    )
    servers = messaging.get("servers")
    if (
        not isinstance(servers, list)
        or not servers
        or not all(
            isinstance(server, str) and server.strip().startswith(("nats://", "tls://"))
            for server in servers
        )
    ):
        raise ConfigError("messaging.servers must list valid NATS endpoints")
    if messaging.get("delivery", "realtime") not in ("realtime", "reliable"):
        raise ConfigError("messaging.delivery must be realtime or reliable")
    if messaging.get("delivery", "realtime") == "reliable":
        if "max_staleness_ms" in messaging:
            raise ConfigError("Reliable messaging forbids max_staleness_ms")
    else:
        _positive(messaging.get("max_staleness_ms"), "messaging.max_staleness_ms")
    for name in ("max_samples_per_batch", "max_batch_bytes", "queue_capacity"):
        if name in messaging:
            value = messaging[name]
            if type(value) is not int or value <= 0:
                raise ConfigError(f"messaging.{name} must be a positive integer")
    if "enqueue_timeout_s" in messaging:
        _positive(messaging["enqueue_timeout_s"], "messaging.enqueue_timeout_s")
    metrics = _section(document, "metrics")
    _reject_unknown(
        metrics,
        {
            "enabled",
            "windows",
            "publish_interval",
            "max_staleness",
            "event_capacity",
            "pending_capacity",
        },
        "metrics",
    )
    if "enabled" in metrics and type(metrics["enabled"]) is not bool:
        raise ConfigError("metrics.enabled must be boolean")
    publish_interval = metrics.get("publish_interval", 60)
    if type(publish_interval) is not int:
        raise ConfigError("metrics.publish_interval must be an integer")
    interval = _positive(publish_interval, "metrics.publish_interval")
    windows = metrics.get("windows", [60, 300, 3600])
    if not isinstance(windows, list) or not windows:
        raise ConfigError("metrics.windows must be a non-empty list")
    for window in windows:
        if type(window) is not int or window <= 0 or window % interval != 0:
            raise ConfigError("metrics.windows must contain multiples of publish_interval")
    for name in ("event_capacity", "pending_capacity"):
        if name in metrics:
            capacity = metrics[name]
            if type(capacity) is not int or capacity <= 0:
                raise ConfigError(f"metrics.{name} must be a positive integer")
    if "max_staleness" in metrics:
        _positive(metrics["max_staleness"], "metrics.max_staleness")


_FLAT_TEMPLATE_NAME = re.compile(
    r"^(?P<template_id>.+)_v(?P<file_version>[0-9]+\.[0-9]+\.[0-9]+)\.yaml$"
)


def _template_references(category: str, folder: Path) -> Iterator[tuple[Path, TemplateRef]]:
    """Yield every versioned template under a category.

    Only the flat ``<category-prefix><id>_vMAJOR.MINOR.PATCH.yaml`` layout is supported.
    Unexpected names are rejected rather than silently skipped.
    """
    for path in sorted(folder.glob("*.yaml")):
        match = _FLAT_TEMPLATE_NAME.match(path.name)
        prefix = TEMPLATE_FILE_PREFIXES[category]
        if match is None or not match["template_id"].startswith(prefix):
            raise ConfigError(f"{path}: invalid {category} filename or version")
        template_id = match["template_id"][len(prefix) :]
        yield path, TemplateRef(category, template_id, match["file_version"])


def _validate_all_templates(root: Path) -> None:
    repository = VersionedTemplateRepository(root)
    for category in ("protocols", "device_models", "point_tables"):
        folder = root / category
        if not folder.is_dir():
            continue
        for path, reference in _template_references(category, folder):
            template = repository.resolve(reference)
            document = template.document
            if category == "protocols":
                if document.get("protocol") not in {"ads", "modbus", "iec104"}:
                    raise ConfigError(f"{path}: unsupported protocol")
            elif category == "device_models":
                if "task" in document:
                    raise ConfigError("Device Model cannot declare Collector Task")
                resolve_device_templates(repository, template)
            elif category == "point_tables":
                if document.get("protocol") not in {"ads", "modbus", "iec104"}:
                    raise ConfigError(f"{path}: point table protocol missing")
                if not isinstance(document.get("points"), Mapping):
                    raise ConfigError(f"{path}: points must be a mapping")
                validate_point_table_chain(repository, template)


def validate_collector_snapshot(paths: SnapshotPaths, *, source_root: Path | None = None) -> None:
    """Validate all common references and the Collector-owned service document."""
    service = read_yaml(paths.service)
    validate_document_version(service, paths.service, supported_schema=6)
    if not isinstance(service, dict):
        raise ConfigError("Collector service configuration must be a mapping")
    allowed = {
        "schema_version",
        "file_version",
        "runtime",
        "watchdog",
        "messaging",
        "metrics",
        "grpc",
        "ads",
        "logs",
        "identity",
        "config_sources",
        "service_id",
    }
    unknown = set(service) - allowed
    if unknown:
        raise ConfigError(f"unknown Collector service fields: {sorted(unknown)}")
    _source_references(service)
    validate_service_settings(service)
    _validate_all_templates(paths.templates)
    shared = SharedDocumentLoader(
        SharedConfigPaths(
            instances=paths.instances,
            templates=paths.templates,
            site_file=paths.site,
            devices_file=paths.devices,
        )
    ).load()
    snapshot = build_shared_snapshot(shared)
    validate_subject_token(str(snapshot.site.site_id))
    for device_identifier, device in snapshot.site.devices.items():
        validate_subject_token(str(device_identifier))
        model = snapshot.device_models[device.device_model_id]
        table = snapshot.point_tables[model.point_table_id]
        for point_id in table.points:
            validate_subject_token(point_id)
    task_file = paths.tasks
    if task_file is None or not task_file.is_file():
        raise ConfigError("Deployment-selected Collector Task Instance is required")
    task_doc = read_yaml(task_file)
    if not isinstance(task_doc, dict) or set(task_doc) != {
        "schema_version",
        "file_version",
        "tasks",
    }:
        raise ConfigError("invalid Collector Task Instance document")
    tasks = task_doc["tasks"]
    if not isinstance(tasks, dict) or not tasks:
        raise ConfigError("Collector Task Instance must define tasks")
    if paths.devices is None:
        raise ConfigError("Deployment-selected Collector Device Instance is required")
    device_document = read_yaml(paths.devices)
    raw_devices = device_document.get("devices")
    if not isinstance(raw_devices, dict):
        raise ConfigError("devices must be a mapping")
    groups: dict[str, dict[str, object]] = {}
    for device_id, raw in raw_devices.items():
        if not isinstance(raw, dict):
            raise ConfigError(f"{device_id}: device must be a mapping")
        group = raw.get("group")
        if not isinstance(group, str) or not group.strip() or group != group.strip():
            raise ConfigError(f"{device_id}: invalid device group")
        groups[group] = raw
    used: set[str] = set()
    for task_id, raw in tasks.items():
        if not isinstance(task_id, str) or not isinstance(raw, dict):
            raise ConfigError("invalid Collector Task")
        if set(raw) != {"group", "point_group", "interval_ms", "desired_state"}:
            raise ConfigError(f"{task_id}: unsupported Task keys")
        group = raw["group"]
        if not isinstance(group, str) or group not in groups or group in used:
            raise ConfigError(f"{task_id}: missing or duplicate device group")
        used.add(group)
        if raw["point_group"] != "all" or raw["desired_state"] not in {"START", "STOP"}:
            raise ConfigError(f"{task_id}: invalid Task selection or desired state")
        interval = raw["interval_ms"]
        if type(interval) is not int or interval < 1:
            raise ConfigError(f"{task_id}: invalid Task interval_ms")
    if set(groups) != used:
        raise ConfigError("some Device groups lack Collector Tasks")


def parse_collector_runtime(
    paths: SnapshotPaths,
    revision: str,
    *,
    task_intents: TaskIntentPort | None = None,
) -> ConfiguredRuntime:
    """Resolve Collector configuration and concrete per-device acquisition plans."""
    validate_collector_snapshot(paths)
    documents = SharedDocumentLoader(
        SharedConfigPaths(
            instances=paths.instances,
            templates=paths.templates,
            site_file=paths.site,
            devices_file=paths.devices,
        )
    ).load()
    snapshot = build_shared_snapshot(documents)
    raw_service = read_yaml(paths.service)
    runtime = raw_service.get("runtime", {})
    watchdog = raw_service.get("watchdog", {})
    if paths.devices is None or paths.tasks is None:
        raise ConfigError("selected Device and Collector Task Instances are required")
    raw_instances = read_yaml(paths.devices)["devices"]
    task_document = read_yaml(paths.tasks)
    raw_tasks = task_document["tasks"]
    tasks_by_group = {item["group"]: (task_id, item) for task_id, item in raw_tasks.items()}
    # Scope operator overrides to the selected immutable Task document.
    task_identity = f"{paths.tasks.name}:{task_document['file_version']}"
    intents = task_intents.read_intents(task_identity) if task_intents is not None else {}
    devices: list[DevicePlan] = []
    for device_id, definition in documents.devices.items():
        if not definition.enabled:
            continue
        config = raw_instances[device_id]
        task_id, task = tasks_by_group[config["group"]]
        intent = intents.get(task_id, task["desired_state"])
        device = snapshot.site.devices[DeviceId(device_id)]
        device_model = snapshot.device_models[device.device_model_id]
        protocol_name = definition.templates.protocol.document["protocol"]
        device, _ = resolve_effective_device(device, device_model, protocol_name)
        point_table_id = snapshot.device_models[device.device_model_id].point_table_id
        point_table = snapshot.point_tables[point_table_id]
        protocol = point_table.protocol.name
        subscribe_enabled = definition.protocol_options.get("subscribe_enabled", False)
        if type(subscribe_enabled) is not bool or (subscribe_enabled and protocol != "ads"):
            raise ConfigError(f"{device.device_id}: invalid Collector subscribe_enabled")
        mode = (
            "subscribe"
            if protocol == "ads" and subscribe_enabled
            else ("listen" if protocol == "iec104" else "poll")
        )
        devices.append(
            DevicePlan(
                device=device,
                point_table=point_table,
                interval=task["interval_ms"] / 1000.0,
                desired_state=intent,
                mode=mode,
                device_type=str(device_model.device_type_id),
                point_table_id=str(point_table_id),
                task_id=validate_subject_token(task_id),
                data_types={
                    point.point_id: snapshot.business_points[
                        point.business_point_id
                    ].data_type.value
                    for point in point_table.points.values()
                },
            )
        )
    return ConfiguredRuntime(
        revision=revision,
        site_id=str(snapshot.site.site_id),
        device_ids=tuple(sorted(str(key) for key in snapshot.site.devices)),
        acquisition=AcquisitionPlan(
            site_id=str(snapshot.site.site_id),
            devices=tuple(devices),
            read_timeout=float(runtime.get("read_timeout", 5)),
            connect_timeout=float(runtime.get("connect_timeout", 10)),
            watchdog_interval=float(watchdog.get("interval", 5)),
            shutdown_timeout=float(runtime.get("shutdown_timeout", 30)),
        ),
    )
