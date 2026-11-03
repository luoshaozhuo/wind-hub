"""Validate complete Sink Service configuration snapshots without side effects."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from sink_service.domain.config import (
    ConfigurationError,
    SnapshotFile,
    ValidatedSnapshot,
    expect_mapping,
    expect_nonempty_str,
    expect_positive_int,
    require_version,
    validate_service,
)


def _yaml_doc(item: SnapshotFile) -> dict[str, Any]:
    try:
        decoded = item.content.decode("utf-8", errors="strict")
        raw = yaml.safe_load(decoded)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"{item.path}: invalid YAML") from exc
    doc = expect_mapping(raw, item.path)
    require_version(doc, item.path)
    return doc


def _template(
    docs: dict[str, dict[str, Any]], family: str, name: str, version: str
) -> dict[str, Any]:
    path = f"templates/{family}/{name}/{version}.yaml"
    item = docs.get(path)
    if item is None:
        raise ConfigurationError(f"missing template: {path}")
    if item.get("template_id") != name or item.get("object_version") != version:
        raise ConfigurationError(f"{path}: template identity does not match reference")
    return item


def _instances(docs: dict[str, dict[str, Any]], family: str) -> dict[str, dict[str, Any]]:
    path = f"instances/{family}.yaml"
    container = docs.get(path)
    if container is None:
        raise ConfigurationError(f"missing required instance file: {path}")
    expect_nonempty_str(container.get("revision"), f"{path}.revision")
    instances = expect_mapping(container.get(family), f"{path}.{family}")
    return {
        key: expect_mapping(value, f"{path}.{family}.{key}") for key, value in instances.items()
    }


def _validate_sink(
    sink_id: str, sink: dict[str, Any], docs: dict[str, dict[str, Any]]
) -> tuple[str, dict[str, Any]]:
    name = expect_nonempty_str(sink.get("template_id"), f"sinks.{sink_id}.template_id")
    version = expect_nonempty_str(sink.get("template_version"), f"sinks.{sink_id}.template_version")
    template = _template(docs, "sinks", name, version)
    kind = template.get("kind")
    if kind not in ("redis", "kafka", "modbus_tcp"):
        raise ConfigurationError(f"sinks.{sink_id}: unsupported Adapter {kind!r}")
    caps = expect_mapping(template.get("capabilities"), f"sinks.{sink_id}.capabilities")
    if any(
        type(caps.get(k)) is not bool
        for k in ("realtime", "reliable", "idempotent", "supports_concurrent_write")
    ):
        raise ConfigurationError(f"sinks.{sink_id}: invalid capability declaration")
    maximum = expect_positive_int(
        caps.get("max_safe_inflight"), f"sinks.{sink_id}.max_safe_inflight"
    )
    if not caps["supports_concurrent_write"] and maximum != 1:
        raise ConfigurationError(f"sinks.{sink_id}: unsafe parallel write declaration")
    if kind == "modbus_tcp" and caps["reliable"]:
        raise ConfigurationError(f"sinks.{sink_id}: Modbus Server cannot be reliable")
    if kind == "kafka" and caps["reliable"]:
        output = expect_mapping(template.get("output"), f"sinks.{sink_id}.output")
        if output.get("business_duplicate_policy") not in ("require_dedup", "allow_at_least_once"):
            raise ConfigurationError(f"sinks.{sink_id}: missing Kafka duplicate policy")
    expect_mapping(sink.get("connection"), f"sinks.{sink_id}.connection")
    expect_mapping(sink.get("overrides"), f"sinks.{sink_id}.overrides")
    if sink["overrides"]:
        raise ConfigurationError(f"sinks.{sink_id}: undeclared overrides are not allowed")
    return str(kind), caps


def _validate_delivery(
    delivery_id: str,
    delivery: dict[str, Any],
    docs: dict[str, dict[str, Any]],
    sinks: dict[str, tuple[str, dict[str, Any]]],
    service: dict[str, Any],
) -> None:
    name = expect_nonempty_str(delivery.get("template_id"), f"deliveries.{delivery_id}.template_id")
    version = expect_nonempty_str(
        delivery.get("template_version"), f"deliveries.{delivery_id}.template_version"
    )
    template = _template(docs, "deliveries", name, version)
    mode = template.get("delivery_mode")
    if mode not in ("realtime", "reliable"):
        raise ConfigurationError(f"deliveries.{delivery_id}: unsupported mode")
    sink_id = expect_nonempty_str(delivery.get("sink_id"), f"deliveries.{delivery_id}.sink_id")
    if sink_id not in sinks:
        raise ConfigurationError(f"deliveries.{delivery_id}: unknown Sink {sink_id}")
    kind, caps = sinks[sink_id]
    if not caps[mode]:
        raise ConfigurationError(f"deliveries.{delivery_id}: incompatible Sink capability")
    if kind == "modbus_tcp" and mode == "reliable":
        raise ConfigurationError(f"deliveries.{delivery_id}: Modbus Server cannot be reliable")
    if delivery.get("desired_state") not in ("START", "STOP"):
        raise ConfigurationError(f"deliveries.{delivery_id}: invalid desired_state")
    source = expect_mapping(delivery.get("source"), f"deliveries.{delivery_id}.source")
    if source.get("wind_farm_id") != service["wind_farm_id"]:
        raise ConfigurationError(f"deliveries.{delivery_id}: wrong wind_farm_id")
    expect_nonempty_str(source.get("collector_id"), f"deliveries.{delivery_id}.collector_id")
    for field in ("devices", "points"):
        values = source.get(field)
        if (
            not isinstance(values, list)
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))
        ):
            raise ConfigurationError(f"deliveries.{delivery_id}: invalid {field}")
    expect_mapping(delivery.get("overrides"), f"deliveries.{delivery_id}.overrides")
    if delivery["overrides"]:
        raise ConfigurationError(f"deliveries.{delivery_id}: undeclared overrides")
    if mode == "realtime":
        output = expect_mapping(template.get("output"), f"deliveries.{delivery_id}.output")
        if output.get("policy") not in ("periodic", "on_change", "change_with_heartbeat"):
            raise ConfigurationError(f"deliveries.{delivery_id}: unknown output policy")
        expect_positive_int(output.get("period_ms"), f"deliveries.{delivery_id}.period_ms")
    if mode == "reliable":
        reliable = expect_mapping(template.get("reliable"), f"deliveries.{delivery_id}.reliable")
        if reliable.get("max_deliver") != -1:
            raise ConfigurationError(f"deliveries.{delivery_id}: bounded reliable retries")
        if reliable.get("dead_letter_subject") != service["nats"]["dead_letter_subject"]:
            raise ConfigurationError(f"deliveries.{delivery_id}: mismatched DLQ subject")


def validate_snapshot(files: tuple[SnapshotFile, ...]) -> ValidatedSnapshot:
    """Resolve all cross-file template references before accepting a snapshot."""
    if not files:
        raise ConfigurationError("empty configuration snapshot")
    if len(files) > 256:
        raise ConfigurationError("configuration file count exceeds 256")
    if sum(len(item.content) for item in files) > 2 * 1024 * 1024:
        raise ConfigurationError("configuration snapshot exceeds 2 MiB")
    by_path: dict[str, SnapshotFile] = {}
    for item in files:
        if item.path in by_path:
            raise ConfigurationError(f"duplicate file: {item.path}")
        by_path[item.path] = item
    docs = {path: _yaml_doc(item) for path, item in by_path.items()}
    service_files = [path for path in docs if path.startswith("services/")]
    if len(service_files) != 2:
        raise ConfigurationError("shared snapshot requires realtime and reliable Service files")
    services = [docs[path] for path in service_files]
    for service_doc in services:
        validate_service(service_doc)
    if {doc["process_role"] for doc in services} != {"realtime", "reliable"}:
        raise ConfigurationError("Service processes must have distinct roles")
    if sum(doc["config_writer"] for doc in services) != 1:
        raise ConfigurationError("configuration domain requires exactly one Writer")
    if len({(doc["grpc"]["listen_host"], doc["grpc"]["listen_port"]) for doc in services}) != 2:
        raise ConfigurationError("Service gRPC ports must be distinct")
    if len({doc["config_root"] for doc in services}) != 1:
        raise ConfigurationError("Service config_root values must match")
    if len({(doc["wind_farm_id"], doc["deployment_id"]) for doc in services}) != 1:
        raise ConfigurationError("Service source identity must match")
    service = next(doc for doc in services if doc["config_writer"])
    sink_instances = _instances(docs, "sinks")
    delivery_instances = _instances(docs, "deliveries")
    sinks = {
        sink_id: _validate_sink(sink_id, definition, docs)
        for sink_id, definition in sink_instances.items()
    }
    for delivery_id, definition in delivery_instances.items():
        _validate_delivery(delivery_id, definition, docs, sinks, service)
    addresses: set[tuple[str, int]] = set()
    for sink_id, (kind, _caps) in sinks.items():
        if kind != "modbus_tcp":
            continue
        address = sink_instances[sink_id]["connection"]
        key = (
            expect_nonempty_str(address.get("listen_host"), f"sinks.{sink_id}.listen_host"),
            expect_positive_int(address.get("port"), f"sinks.{sink_id}.port", 65535),
        )
        if key in addresses:
            raise ConfigurationError(f"sinks.{sink_id}: duplicate Modbus listen address")
        addresses.add(key)
    # Validate the complete Modbus value/quality address closure per Sink.
    for sink_id, (kind, _caps) in sinks.items():
        if kind != "modbus_tcp":
            continue
        sink = sink_instances[sink_id]
        template = _template(docs, "sinks", sink["template_id"], sink["template_version"])
        output = expect_mapping(template.get("output"), f"sinks.{sink_id}.output")
        if output.get("value_encoding") != "float32_be":
            raise ConfigurationError(f"sinks.{sink_id}: unsupported value encoding")
        if output.get("quality_encoding") != "separate_register":
            raise ConfigurationError(f"sinks.{sink_id}: missing quality registers")
        if output.get("quality_codes") != {"good": 0, "bad": 1, "uncertain": 2}:
            raise ConfigurationError(f"sinks.{sink_id}: invalid quality codes")
        expect_positive_int(output.get("unit_id"), f"sinks.{sink_id}.unit_id", 247)
        start = output.get("register_start")
        quality_start = output.get("quality_register_start")
        if type(start) is not int or not 0 <= start <= 65535:
            raise ConfigurationError(f"sinks.{sink_id}: invalid register_start")
        if type(quality_start) is not int or not 0 <= quality_start <= 65535:
            raise ConfigurationError(f"sinks.{sink_id}: invalid quality_register_start")
        sources: set[tuple[str, str, str]] = set()
        for delivery_id, delivery in delivery_instances.items():
            if delivery["sink_id"] != sink_id:
                continue
            source = delivery["source"]
            devices, points = source["devices"], source["points"]
            if not devices or not points:
                raise ConfigurationError(
                    f"deliveries.{delivery_id}: Modbus requires bounded explicit point map"
                )
            for device_id in devices:
                for point_id in points:
                    point_key = (source["collector_id"], device_id, point_id)
                    if point_key in sources:
                        raise ConfigurationError(
                            f"sinks.{sink_id}: duplicate output point {point_key}"
                        )
                    sources.add(point_key)
        width = len(sources)
        if width == 0:
            continue
        value_end = start + 2 * width - 1
        quality_end = quality_start + width - 1
        if max(value_end, quality_end) > 65535:
            raise ConfigurationError(f"sinks.{sink_id}: Modbus register overflow")
        if start <= quality_end and quality_start <= value_end:
            raise ConfigurationError(f"sinks.{sink_id}: value/quality registers overlap")
    digest = hashlib.sha256()
    for path, item in sorted(by_path.items()):
        digest.update(path.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(item.content)
        digest.update(b"\x00")
    return ValidatedSnapshot(
        revision=digest.hexdigest(),
        files=tuple(by_path[path] for path in sorted(by_path)),
        service=service,
        sinks=sink_instances,
        deliveries=delivery_instances,
    )


def read_snapshot(root: Path, service_path: Path) -> ValidatedSnapshot:
    """Read only expected YAML files, rejecting symlink escapes."""
    root = root.resolve(strict=True)
    service_path = service_path.resolve(strict=True)
    if not service_path.is_relative_to(root):
        raise ConfigurationError("Service path escapes configuration root")
    paths = list((root / "services").rglob("*.yaml"))
    if service_path not in [p.resolve(strict=True) for p in paths]:
        raise ConfigurationError("selected Service is absent from shared snapshot")
    for category in ("templates", "instances"):
        directory = root / category
        if directory.exists():
            paths.extend(directory.rglob("*.yaml"))
    files: list[SnapshotFile] = []
    for path in paths:
        if path.is_symlink() or any(
            parent.is_symlink() for parent in path.parents if parent != root
        ):
            raise ConfigurationError(f"symbolic-link configuration path: {path}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ConfigurationError(f"unsafe configuration file: {path}")
        files.append(SnapshotFile(resolved.relative_to(root).as_posix(), resolved.read_bytes()))
    return validate_snapshot(tuple(files))


def snapshot_from_wire(files: list[tuple[str, bytes, int, str]]) -> ValidatedSnapshot:
    """Validate transport file metadata before parsing any YAML payload."""
    items: list[SnapshotFile] = []
    for path, content, size, expected_hash in files:
        if size != len(content):
            raise ConfigurationError(f"{path}: size_bytes mismatch")
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise ConfigurationError(f"{path}: sha256 mismatch")
        items.append(SnapshotFile(path, content))
    return validate_snapshot(tuple(items))
