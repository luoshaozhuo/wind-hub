"""Core-backed validation for Collector's full configuration snapshot."""

from __future__ import annotations

import math
import re
from collections.abc import Iterator, Mapping
from pathlib import Path

from collector.application.acquisition import AcquisitionPlan, DevicePlan
from collector.application.config_models import SnapshotPaths
from collector.application.lifecycle import ConfiguredRuntime
from contracts.sample_batch import validate_subject_token
from core.application.errors import ConfigError
from core.configuration import SharedConfigPaths, SharedDocumentLoader, build_shared_snapshot
from core.configuration.document_version import validate_document_version
from core.configuration.effective import resolve_effective_device
from core.configuration.template_graph import (
    resolve_model_templates,
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
    """Resolve only paths explicitly named by the service configuration.

    Paths are relative to the config root (the parent of services/), never
    the process working directory. Reject symlinks and escaping paths.
    """
    service_file = config_file.absolute()
    if service_file.parent.name != "services":
        raise ConfigError("Collector service file must reside in a services directory")
    config_root = service_file.parent.parent.resolve()
    if not service_file.is_file() or service_file.is_symlink() or service_file.parent.is_symlink():
        raise ConfigError("Collector service file is missing or unsafe")
    service = read_yaml(service_file)
    references = _source_references(service)

    def resolve(value: str, *, directory: bool) -> Path:
        requested = Path(value)
        if requested.is_absolute():
            candidate = requested
        elif requested.parts and requested.parts[0] == "configs":
            candidate = config_root.joinpath(*requested.parts[1:])
        else:
            candidate = config_root / requested
        # Do not allow a symlink in any existing path segment.
        current = candidate
        while current != config_root and current != current.parent:
            if current.is_symlink():
                raise ConfigError(f"configuration reference uses symlink: {value}")
            current = current.parent
        target = candidate.resolve()
        if not target.is_relative_to(config_root):
            raise ConfigError(f"configuration reference escapes config root: {value}")
        if not (target.is_dir() if directory else target.is_file()):
            raise ConfigError(f"configuration reference missing or invalid: {value}")
        return target

    site, devices, templates = references
    site_path = resolve(site, directory=False)
    devices_path = resolve(devices, directory=False)
    template_root = resolve(templates, directory=True)
    if site_path == devices_path:
        raise ConfigError("site and devices must reference different instance files")
    return SnapshotPaths(
        service=service_file,
        instances=config_root,
        templates=template_root,
        site=site_path,
        devices=devices_path,
    )


def _source_references(service: Mapping[str, object]) -> tuple[str, str, str]:
    sources = _section(service, "config_sources")
    if set(sources) != {"instances", "templates"}:
        raise ConfigError("config_sources requires exactly instances and templates")
    instances = sources["instances"]
    if not isinstance(instances, dict) or set(instances) != {"site", "devices"}:
        raise ConfigError("config_sources.instances requires exactly site and devices")
    templates = sources["templates"]
    if not isinstance(templates, dict) or set(templates) != {"root"}:
        raise ConfigError("config_sources.templates requires exactly root")
    site = instances["site"]
    devices = instances["devices"]
    template_root = templates["root"]
    if not all(isinstance(value, str) for value in (site, devices, template_root)):
        raise ConfigError("config_sources paths must be strings")
    if any(
        not value.strip() or chr(0) in value or chr(92) in value or ".." in Path(value).parts
        for value in (site, devices, template_root)
    ):
        raise ConfigError("config_sources paths must be safe, nonempty strings")
    return site, devices, template_root


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
    if "max_staleness_ms" in messaging:
        _positive(messaging["max_staleness_ms"], "messaging.max_staleness_ms")
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
    for category in ("protocols", "device_models", "point_tables", "tasks"):
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
                resolve_model_templates(repository, template)
            elif category == "point_tables":
                if document.get("protocol") not in {"ads", "modbus", "iec104"}:
                    raise ConfigError(f"{path}: point table protocol missing")
                if not isinstance(document.get("points"), Mapping):
                    raise ConfigError(f"{path}: points must be a mapping")
                validate_point_table_chain(repository, template)
            elif category == "tasks":
                interval = document.get("interval")
                if isinstance(interval, bool) or not isinstance(interval, int | float):
                    raise ConfigError(f"{path}: task interval must be numeric")
                _positive(interval, f"{path}.interval")
                if document.get("point_group") != "all":
                    raise ConfigError(f"{path}: Collector tasks must select all dynamic points")
                if document.get("desired_state", "STOP") not in ("START", "STOP"):
                    raise ConfigError(f"{path}: invalid task default desired_state")


def validate_collector_snapshot(paths: SnapshotPaths, *, source_root: Path | None = None) -> None:
    """Validate all common references and the Collector-owned service document."""
    service = read_yaml(paths.service)
    validate_document_version(service, paths.service)
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
    }
    unknown = set(service) - allowed
    if unknown:
        raise ConfigError(f"unknown Collector service fields: {sorted(unknown)}")
    references = _source_references(service)
    if source_root is not None:
        authorized_root = source_root.resolve()
        for value in references:
            requested = Path(value)
            if requested.is_absolute() and not requested.resolve().is_relative_to(authorized_root):
                raise ConfigError("absolute service reference escapes config root")
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
    templates = VersionedTemplateRepository(paths.templates)
    device_document = read_yaml(paths.devices or paths.instances / "devices.yaml")
    _reject_unknown(
        device_document, {"schema_version", "file_version", "devices"}, "devices document"
    )
    raw_devices = device_document.get("devices")
    if not isinstance(raw_devices, dict):
        raise ConfigError("devices must be a mapping")
    known_ids = {str(identifier) for identifier in snapshot.site.devices}
    for device_id, definition in shared.devices.items():
        model_templates = resolve_model_templates(templates, definition.templates.model)
        task_document = model_templates.task.document
        interval = task_document.get("interval")
        if isinstance(interval, bool) or not isinstance(interval, int | float) or interval <= 0:
            raise ConfigError(f"{device_id}: invalid acquisition interval")
        if task_document.get("desired_state", "STOP") not in ("START", "STOP"):
            raise ConfigError(f"{device_id}: invalid task default desired state")
        point_group = task_document.get("point_group")
        if point_group != "all":
            raise ConfigError(f"{device_id}: Collector task must collect all dynamic points")
        instance = raw_devices[device_id]
        if not isinstance(instance, dict):
            raise ConfigError(f"{device_id}: invalid device instance")
        overrides = instance.get("overrides", {})
        if not isinstance(overrides, dict) or set(overrides) - {"protocol_options", "task"}:
            raise ConfigError(f"{device_id}: unsupported device overrides")
        task_override = overrides.get("task", {})
        if not isinstance(task_override, dict) or set(task_override) - {"desired_state"}:
            raise ConfigError(f"{device_id}: unsupported task overrides")
        if task_override.get("desired_state", "STOP") not in ("START", "STOP"):
            raise ConfigError(f"{device_id}: invalid task desired_state")
        if device_id not in known_ids:
            raise ConfigError(f"{device_id}: unresolved device in shared snapshot")


def parse_collector_runtime(paths: SnapshotPaths, revision: str) -> ConfiguredRuntime:
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
    template_repo = VersionedTemplateRepository(paths.templates)
    raw_instances = read_yaml(paths.devices or paths.instances / "devices.yaml")["devices"]
    devices: list[DevicePlan] = []
    for device_id, definition in documents.devices.items():
        if not definition.enabled:
            continue
        model = resolve_model_templates(template_repo, definition.templates.model)
        config = raw_instances[device_id]
        overrides = config.get("overrides", {})
        task_overrides = overrides.get("task", {})
        intent = task_overrides.get(
            "desired_state", model.task.document.get("desired_state", "STOP")
        )
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
                interval=float(model.task.document["interval"]),
                desired_state=intent,
                mode=mode,
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
