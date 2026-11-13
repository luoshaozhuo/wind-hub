"""Validate complete Sink Service configuration snapshots without side effects."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from contracts.sample_batch import validate_subject_token
from core.infrastructure.config.generic_yaml import ConfigOperationError, parse_yaml
from sink_service.domain.config import (
    ConfigurationError,
    SnapshotFile,
    ValidatedSnapshot,
    config_sources,
    expect_mapping,
    expect_nonempty_str,
    expect_positive_int,
    require_keys,
    require_version,
    validate_service,
)


def _yaml_doc(item: SnapshotFile) -> dict[str, Any]:
    try:
        decoded = item.content.decode("utf-8", errors="strict")
        raw = parse_yaml(decoded)
    except (UnicodeError, ConfigOperationError) as exc:
        raise ConfigurationError(f"{item.path}: invalid YAML") from exc
    doc = expect_mapping(raw, item.path)
    require_version(doc, item.path)
    if item.path.startswith("configs/templates/sinks/"):
        require_keys(
            doc,
            item.path,
            frozenset(
                {
                    "schema_version",
                    "file_version",
                    "template_id",
                    "kind",
                    "capabilities",
                    "connection",
                    "output",
                }
            ),
        )
    elif item.path.startswith("configs/templates/deliveries/"):
        require_keys(
            doc,
            item.path,
            frozenset(
                {
                    "schema_version",
                    "file_version",
                    "template_id",
                    "delivery_mode",
                    "source_filter",
                }
            ),
            frozenset({"output", "staleness", "reliable"}),
        )
    return doc


def template_path(template_root: str, family: str, name: str, version: str) -> str:
    """Resolve an exact template version without directory scanning."""
    if family not in {"sinks", "deliveries"}:
        raise ConfigurationError("unsupported template family")
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", name) is None:
        raise ConfigurationError(f"invalid template_id: {name!r}")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ConfigurationError(f"invalid template_version: {version!r}")
    return f"{template_root}/{family}/{name}_v{version}.yaml"


def template_document(
    snapshot: ValidatedSnapshot, family: str, name: str, version: str
) -> dict[str, Any]:
    """Load one validated snapshot template by exact identity and version."""
    _, template_root = config_sources(snapshot.service)
    path = template_path(template_root, family, name, version)
    for item in snapshot.files:
        if item.path == path:
            document = _yaml_doc(item)
            if document.get("template_id") != name or document.get("file_version") != version:
                raise ConfigurationError(f"{path}: template identity mismatch")
            return document
    raise ConfigurationError(f"missing referenced template: {path}")


def _template(
    docs: dict[str, dict[str, Any]],
    template_root: str,
    family: str,
    name: str,
    version: str,
) -> dict[str, Any]:
    path = template_path(template_root, family, name, version)
    item = docs.get(path)
    if item is None:
        raise ConfigurationError(f"missing template: {path}")
    if item.get("template_id") != name or item.get("file_version") != version:
        raise ConfigurationError(f"{path}: template identity does not match reference")
    return item


def _instances(
    docs: dict[str, dict[str, Any]], family: str, path: str
) -> dict[str, dict[str, Any]]:
    container = docs.get(path)
    if container is None:
        raise ConfigurationError(f"missing required instance file: {path}")
    expect_nonempty_str(container.get("revision"), f"{path}.revision")
    require_keys(
        container,
        path,
        frozenset({"schema_version", "file_version", "revision", family}),
    )
    instances = expect_mapping(container.get(family), f"{path}.{family}")
    return {
        key: expect_mapping(value, f"{path}.{family}.{key}") for key, value in instances.items()
    }


def _validate_sink(
    sink_id: str, sink: dict[str, Any], docs: dict[str, dict[str, Any]], template_root: str
) -> tuple[str, dict[str, Any]]:
    name = expect_nonempty_str(sink.get("template_id"), f"sinks.{sink_id}.template_id")
    version = expect_nonempty_str(sink.get("template_version"), f"sinks.{sink_id}.template_version")
    template = _template(docs, template_root, "sinks", name, version)
    require_keys(
        sink,
        f"sinks.{sink_id}",
        frozenset({"template_id", "template_version", "connection", "overrides"}),
    )
    kind = template.get("kind")
    if kind not in ("redis", "kafka", "modbus_tcp", "gateway"):
        raise ConfigurationError(f"sinks.{sink_id}: unsupported Adapter {kind!r}")
    caps = expect_mapping(template.get("capabilities"), f"sinks.{sink_id}.capabilities")
    require_keys(
        caps,
        f"sinks.{sink_id}.capabilities",
        frozenset(
            {
                "realtime",
                "reliable",
                "idempotent",
                "supports_concurrent_write",
                "max_safe_inflight",
            }
        ),
    )
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
    if kind == "kafka":
        if caps["realtime"] or not caps["reliable"]:
            raise ConfigurationError(f"sinks.{sink_id}: Kafka must be Reliable-only")
    elif not caps["realtime"] or caps["reliable"]:
        raise ConfigurationError(f"sinks.{sink_id}: {kind} must be Realtime-only")
    if kind == "kafka" and caps["reliable"]:
        output = expect_mapping(template.get("output"), f"sinks.{sink_id}.output")
        policy = output.get("business_duplicate_policy")
        if policy not in ("require_dedup", "allow_at_least_once"):
            raise ConfigurationError(f"sinks.{sink_id}: missing Kafka duplicate policy")
        if policy == "require_dedup":
            identity = output.get("dedup_identity_fields")
            if (
                not isinstance(identity, list)
                or len(identity) != 2
                or any(not isinstance(field, str) for field in identity)
                or set(identity) != {"delivery_id", "sample_id"}
            ):
                raise ConfigurationError(
                    f"sinks.{sink_id}: Kafka downstream dedup identity must "
                    "contain delivery_id and sample_id"
                )
    connection = expect_mapping(sink.get("connection"), f"sinks.{sink_id}.connection")
    connection_keys = {
        "redis": {"host", "port"},
        "kafka": {"bootstrap_servers"},
        "modbus_tcp": {"listen_host", "port"},
        "gateway": {"endpoint", "gateway_id", "credential_ref"},
    }
    require_keys(
        connection,
        f"sinks.{sink_id}.connection",
        frozenset(connection_keys[str(kind)]),
        frozenset({"credential_ref"}),
    )
    output = expect_mapping(template.get("output"), f"sinks.{sink_id}.output")
    output_keys = {
        "redis": {"namespace", "write_timeout_ms"},
        "kafka": {
            "topic",
            "key_field",
            "business_duplicate_policy",
            "dedup_identity_fields",
            "enable_idempotence",
            "acks",
            "write_timeout_ms",
        },
        "gateway": {"write_timeout_ms", "lease_ms", "max_batch_size"},
        "modbus_tcp": {
            "unit_id",
            "register_start",
            "value_encoding",
            "quality_encoding",
            "quality_register_start",
            "quality_codes",
            "write_timeout_ms",
        },
    }
    require_keys(
        output,
        f"sinks.{sink_id}.output",
        frozenset(output_keys[str(kind)]),
        frozenset({"linger_ms", "max_batch_size"} if kind == "kafka" else ()),
    )
    expect_positive_int(output.get("write_timeout_ms"), f"sinks.{sink_id}.write_timeout_ms", 30000)
    reference = connection.get("credential_ref", "")
    if not isinstance(reference, str):
        raise ConfigurationError(f"sinks.{sink_id}: credential_ref must be a string")
    if reference and not reference.startswith("env:"):
        raise ConfigurationError(f"sinks.{sink_id}: unsupported Secret reference type")
    if kind == "redis":
        expect_nonempty_str(connection.get("host"), f"sinks.{sink_id}.host")
        expect_positive_int(connection.get("port"), f"sinks.{sink_id}.port", 65535)
        namespace = expect_nonempty_str(output.get("namespace"), f"sinks.{sink_id}.namespace")
        if any(char.isspace() for char in namespace):
            raise ConfigurationError(f"sinks.{sink_id}: invalid Redis namespace")
    elif kind == "kafka":
        brokers = connection.get("bootstrap_servers")
        if (
            not isinstance(brokers, list)
            or not brokers
            or any(not isinstance(server, str) or not server for server in brokers)
        ):
            raise ConfigurationError(f"sinks.{sink_id}: invalid Kafka brokers")
        expect_nonempty_str(output.get("topic"), f"sinks.{sink_id}.topic")
        if output.get("acks") != "all" or output.get("enable_idempotence") is not True:
            raise ConfigurationError(f"sinks.{sink_id}: Kafka broker ACK contract mismatch")
        linger_ms = output.get("linger_ms", 0)
        if type(linger_ms) is not int or linger_ms != 0:
            raise ConfigurationError(f"sinks.{sink_id}: Kafka linger_ms must be 0")
        expect_positive_int(output.get("max_batch_size", 131072), f"sinks.{sink_id}.max_batch_size")
    elif kind == "gateway":
        endpoint = expect_nonempty_str(connection.get("endpoint"), f"sinks.{sink_id}.endpoint")
        host, delimiter, port = endpoint.rpartition(":")
        if not delimiter or not host.strip() or not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ConfigurationError(f"sinks.{sink_id}: Gateway endpoint must be host:port")
        expect_nonempty_str(connection.get("gateway_id"), f"sinks.{sink_id}.gateway_id")
        if not reference:
            raise ConfigurationError(f"sinks.{sink_id}: Gateway requires mTLS credential_ref")
        expect_positive_int(output.get("lease_ms"), f"sinks.{sink_id}.lease_ms", 60000)
        if output["lease_ms"] < 1000:
            raise ConfigurationError(f"sinks.{sink_id}: lease_ms below 1000")
        expect_positive_int(output.get("max_batch_size"), f"sinks.{sink_id}.max_batch_size", 500)
    elif kind == "modbus_tcp":
        expect_nonempty_str(connection.get("listen_host"), f"sinks.{sink_id}.listen_host")
        expect_positive_int(connection.get("port"), f"sinks.{sink_id}.port", 65535)
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
    template_root: str,
) -> None:
    name = expect_nonempty_str(delivery.get("template_id"), f"deliveries.{delivery_id}.template_id")
    version = expect_nonempty_str(
        delivery.get("template_version"), f"deliveries.{delivery_id}.template_version"
    )
    template = _template(docs, template_root, "deliveries", name, version)
    require_keys(
        delivery,
        f"deliveries.{delivery_id}",
        frozenset(
            {
                "template_id",
                "template_version",
                "sink_id",
                "desired_state",
                "source",
                "overrides",
            }
        ),
        frozenset({"target_mappings"}),
    )
    source_filter = expect_mapping(
        template.get("source_filter"), f"deliveries.{delivery_id}.source_filter"
    )
    if source_filter:
        raise ConfigurationError(f"deliveries.{delivery_id}: unsupported source_filter entries")
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
    require_keys(
        source,
        f"deliveries.{delivery_id}.source",
        frozenset({"wind_farm_id", "collector_id", "devices", "points"}),
    )
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
    if kind == "gateway":
        if mode != "realtime":
            raise ConfigurationError(f"deliveries.{delivery_id}: Gateway requires Realtime")
        if not source["devices"] or not source["points"]:
            raise ConfigurationError(f"deliveries.{delivery_id}: Gateway needs bounded sources")
        mappings = delivery.get("target_mappings")
        if not isinstance(mappings, list) or not mappings:
            raise ConfigurationError(f"deliveries.{delivery_id}: missing Gateway mappings")
        target_ids: set[str] = set()
        for index, raw_mapping in enumerate(mappings):
            mapping = expect_mapping(raw_mapping, f"deliveries.{delivery_id}.mapping[{index}]")
            require_keys(
                mapping,
                f"deliveries.{delivery_id}.mapping[{index}]",
                frozenset({"source_device_id", "source_point_id", "variable_id"}),
            )
            for field in ("source_device_id", "source_point_id", "variable_id"):
                expect_nonempty_str(mapping.get(field), f"deliveries.{delivery_id}.{field}")
            if (
                mapping["source_device_id"] not in source["devices"]
                or mapping["source_point_id"] not in source["points"]
            ):
                raise ConfigurationError(f"deliveries.{delivery_id}: mapping outside source")
            variable_id = mapping["variable_id"]
            if variable_id in target_ids:
                raise ConfigurationError(f"deliveries.{delivery_id}: duplicate Gateway variable")
            target_ids.add(variable_id)
    elif "target_mappings" in delivery:
        raise ConfigurationError(f"deliveries.{delivery_id}: unexpected target_mappings")
    for device in source["devices"]:
        try:
            validate_subject_token(device)
        except ValueError as exc:
            raise ConfigurationError(
                f"deliveries.{delivery_id}: invalid source device token"
            ) from exc
    expect_mapping(delivery.get("overrides"), f"deliveries.{delivery_id}.overrides")
    if delivery["overrides"]:
        raise ConfigurationError(f"deliveries.{delivery_id}: undeclared overrides")
    if mode == "realtime":
        output = expect_mapping(template.get("output"), f"deliveries.{delivery_id}.output")
        require_keys(
            template,
            f"deliveries.{delivery_id}.template",
            frozenset(
                {
                    "schema_version",
                    "file_version",
                    "template_id",
                    "delivery_mode",
                    "source_filter",
                    "output",
                }
            ),
            frozenset({"staleness"}),
        )
        require_keys(
            output,
            f"deliveries.{delivery_id}.output",
            frozenset({"policy", "period_ms"}),
            frozenset({"absolute_threshold", "relative_threshold"}),
        )
        if output.get("policy") not in ("periodic", "on_change", "change_with_heartbeat"):
            raise ConfigurationError(f"deliveries.{delivery_id}: unknown output policy")
        expect_positive_int(output.get("period_ms"), f"deliveries.{delivery_id}.period_ms")
        for threshold in ("absolute_threshold", "relative_threshold"):
            value = output.get(threshold, 0.0)
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not 0 <= value < float("inf")
            ):
                raise ConfigurationError(f"deliveries.{delivery_id}: invalid {threshold}")
        staleness = expect_mapping(
            template.get("staleness", {}), f"deliveries.{delivery_id}.staleness"
        )
        require_keys(
            staleness,
            f"deliveries.{delivery_id}.staleness",
            frozenset(),
            frozenset({"max_age_ms"}),
        )
        if "max_age_ms" in staleness:
            expect_positive_int(
                staleness["max_age_ms"],
                f"deliveries.{delivery_id}.max_age_ms",
            )
    if mode == "reliable":
        require_keys(
            template,
            f"deliveries.{delivery_id}.template",
            frozenset(
                {
                    "schema_version",
                    "file_version",
                    "template_id",
                    "delivery_mode",
                    "source_filter",
                    "reliable",
                }
            ),
        )
        if kind == "kafka" and caps["supports_concurrent_write"] and len(source["devices"]) != 1:
            raise ConfigurationError(
                f"deliveries.{delivery_id}: concurrent Kafka requires one explicit device"
            )
        reliable = expect_mapping(template.get("reliable"), f"deliveries.{delivery_id}.reliable")
        require_keys(
            reliable,
            f"deliveries.{delivery_id}.reliable",
            frozenset(
                {
                    "max_deliver",
                    "retry_base_ms",
                    "retry_max_ms",
                    "dead_letter_subject",
                }
            ),
        )
        if reliable.get("max_deliver") != -1:
            raise ConfigurationError(f"deliveries.{delivery_id}: bounded reliable retries")
        if reliable.get("dead_letter_subject") != service["nats"]["dead_letter_subject"]:
            raise ConfigurationError(f"deliveries.{delivery_id}: mismatched DLQ subject")


def _source_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Empty selectors mean all devices/points, not an empty output set."""
    if (
        left["wind_farm_id"] != right["wind_farm_id"]
        or left["collector_id"] != right["collector_id"]
    ):
        return False
    for field in ("devices", "points"):
        lhs, rhs = set(left[field]), set(right[field])
        if lhs and rhs and lhs.isdisjoint(rhs):
            return False
    return True


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
    service_files = [path for path in docs if path.startswith("configs/services/")]
    if service_files != ["configs/services/sink_service.yaml"]:
        raise ConfigurationError("expected one sink_service.yaml")
    service = docs[service_files[0]]
    validate_service(service)
    source_paths, template_root = config_sources(service)
    sink_instances = _instances(docs, "sinks", source_paths["sinks"])
    delivery_instances = _instances(docs, "deliveries", source_paths["deliveries"])
    required = set(service_files) | set(source_paths.values())
    for family, definitions in (("sinks", sink_instances), ("deliveries", delivery_instances)):
        for identifier, definition in definitions.items():
            name = expect_nonempty_str(
                definition.get("template_id"), f"{family}.{identifier}.template_id"
            )
            version = expect_nonempty_str(
                definition.get("template_version"), f"{family}.{identifier}.template_version"
            )
            required.add(template_path(template_root, family, name, version))
    if set(by_path) != required:
        raise ConfigurationError(
            f"snapshot file closure mismatch: missing={sorted(required - set(by_path))}, "
            f"unexpected={sorted(set(by_path) - required)}"
        )
    sinks = {
        sink_id: _validate_sink(sink_id, definition, docs, template_root)
        for sink_id, definition in sink_instances.items()
    }
    for delivery_id, definition in delivery_instances.items():
        _validate_delivery(delivery_id, definition, docs, sinks, service, template_root)
    # Two Delivery source selectors may share one physical Sink only when
    # they cannot write the same logical point. Empty selectors are wildcards.
    # The check also covers distinct Redis/Kafka Sink IDs with identical
    # declared physical output endpoints.
    outputs: dict[tuple[object, ...], list[tuple[str, dict[str, Any]]]] = {}
    for delivery_id, delivery in delivery_instances.items():
        sink_id = delivery["sink_id"]
        kind, _ = sinks[sink_id]
        if kind == "modbus_tcp":
            continue  # The register closure below validates Modbus explicitly.
        sink = sink_instances[sink_id]
        conn = sink["connection"]
        template = _template(
            docs, template_root, "sinks", sink["template_id"], sink["template_version"]
        )
        output = template["output"]
        if kind == "redis":
            address: tuple[object, ...] = (kind, conn["host"], conn["port"], output["namespace"])
        elif kind == "gateway":
            address = (kind, conn["endpoint"], conn["gateway_id"])
        else:
            address = (kind, tuple(sorted(conn["bootstrap_servers"])), output["topic"])
        existing = outputs.setdefault(address, [])
        for previous_id, previous_source in existing:
            previous_sink = delivery_instances[previous_id]["sink_id"]
            if kind == "kafka" and (
                sinks[sink_id][1]["supports_concurrent_write"]
                or sinks[previous_sink][1]["supports_concurrent_write"]
            ):
                if sink_id != previous_sink:
                    raise ConfigurationError(
                        f"deliveries.{delivery_id}: concurrent Kafka target must "
                        f"reuse Sink {previous_sink}"
                    )
                left = delivery["source"]
                right = previous_source
                left_devices = set(left["devices"])
                right_devices = set(right["devices"])
                if (
                    left["wind_farm_id"] == right["wind_farm_id"]
                    and left["collector_id"] == right["collector_id"]
                    and (
                        not left_devices
                        or not right_devices
                        or not left_devices.isdisjoint(right_devices)
                    )
                ):
                    raise ConfigurationError(
                        f"deliveries.{delivery_id}: concurrent Kafka device overlaps "
                        f"deliveries.{previous_id} at the same Kafka target"
                    )
            if _source_overlap(delivery["source"], previous_source):
                raise ConfigurationError(
                    f"deliveries.{delivery_id}: output point overlaps "
                    f"deliveries.{previous_id} at the same {kind} target"
                )
        existing.append((delivery_id, delivery["source"]))
    addresses: set[tuple[str, int]] = set()
    for sink_id, (kind, _caps) in sinks.items():
        if kind != "modbus_tcp":
            continue
        connection = sink_instances[sink_id]["connection"]
        key = (
            expect_nonempty_str(connection.get("listen_host"), f"sinks.{sink_id}.listen_host"),
            expect_positive_int(connection.get("port"), f"sinks.{sink_id}.port", 65535),
        )
        if key in addresses:
            raise ConfigurationError(f"sinks.{sink_id}: duplicate Modbus listen address")
        addresses.add(key)
    # Validate the complete Modbus value/quality address closure per Sink.
    for sink_id, (kind, _caps) in sinks.items():
        if kind != "modbus_tcp":
            continue
        sink = sink_instances[sink_id]
        template = _template(
            docs, template_root, "sinks", sink["template_id"], sink["template_version"]
        )
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
    """Load the canonical Main Service and its referenced configuration closure."""
    root = root.resolve(strict=True)
    selected: list[str] = []
    for path in (service_path,):
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise ConfigurationError("Service path escapes configuration root")
        relative = resolved.relative_to(root).as_posix()
        if not relative.startswith("configs/services/") or not relative.endswith(".yaml"):
            raise ConfigurationError("Service must be an explicit Service pathname")
        selected.append(relative)
    if selected != ["configs/services/sink_service.yaml"]:
        raise ConfigurationError("canonical Service pathname required")

    def safe_file(pathname: str) -> SnapshotFile:
        target = root / pathname
        if target.is_symlink() or any(
            parent.is_symlink() for parent in target.parents if parent != root
        ):
            raise ConfigurationError(f"symbolic-link configuration path: {pathname}")
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ConfigurationError(f"unsafe configuration file: {pathname}")
        return SnapshotFile(pathname, resolved.read_bytes())

    service_paths = tuple(selected)
    files = [safe_file(path) for path in service_paths]
    service = _yaml_doc(files[0])
    validate_service(service)
    instance_paths, template_root = config_sources(service)
    files.extend(safe_file(path) for path in instance_paths.values())
    docs = {item.path: _yaml_doc(item) for item in files}
    required: set[str] = set()
    for family, pathname in instance_paths.items():
        entries = _instances(docs, family, pathname)
        for identifier, value in entries.items():
            name = expect_nonempty_str(
                value.get("template_id"), f"{family}.{identifier}.template_id"
            )
            version = expect_nonempty_str(
                value.get("template_version"), f"{family}.{identifier}.template_version"
            )
            required.add(template_path(template_root, family, name, version))
    files.extend(safe_file(path) for path in sorted(required))
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
