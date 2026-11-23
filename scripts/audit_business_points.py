"""Audit BusinessPoint and PointTable configuration without guessing PLC semantics."""

from __future__ import annotations

import argparse
import csv
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml


class _StrictLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently replacing earlier values."""


def _strict_mapping(loader: _StrictLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ValueError(f"YAML mapping key must be a string: {key!r}")
        if key in mapping:
            raise ValueError(f"duplicate YAML mapping key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_mapping)

_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_TABLE_FILE = re.compile(r"^point_table_(?P<id>.+)_v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)\.yaml$")


def _load(path: Path, root: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError(f"unsafe configuration file: {path}")
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)
    if not isinstance(document, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    if type(document.get("schema_version")) is not int:
        raise ValueError(f"missing integer schema_version: {path}")
    version = document.get("file_version")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ValueError(f"invalid file_version: {path}")
    return document


def _validate_references(root: Path, documents: dict[Path, dict[str, Any]]) -> None:
    """Verify exact versioned DeviceModel, PointTable and protocol references."""
    template_root = root / "templates"
    for path, document in documents.items():
        if path.parent == template_root / "device_models":
            for category, prefix in (
                ("protocol", "protocol"),
                ("point_table", "point_table"),
            ):
                reference = document.get(category)
                if not isinstance(reference, dict):
                    raise ValueError(f"missing {category} reference: {path}")
                identity, version = reference.get("id"), reference.get("version")
                if not isinstance(identity, str) or not isinstance(version, str):
                    raise ValueError(f"invalid {category} reference: {path}")
                if _VERSION.fullmatch(version) is None or "/" in identity or ".." in identity:
                    raise ValueError(f"unsafe {category} reference: {path}")
                target = template_root / f"{category}s" / f"{prefix}_{identity}_v{version}.yaml"
                if target not in documents:
                    raise ValueError(f"missing {category} target {target} from {path}")
        if path.parent == root / "instances" and path.name.startswith("device_"):
            devices = document.get("devices")
            if not isinstance(devices, dict):
                raise ValueError(f"missing device entries: {path}")
            for device_id, device in devices.items():
                if not isinstance(device, dict) or not isinstance(device.get("model"), dict):
                    raise ValueError(f"invalid model for device {device_id}: {path}")
                model = device["model"]
                identity, version = model.get("id"), model.get("version")
                if not isinstance(identity, str) or not isinstance(version, str):
                    raise ValueError(f"invalid model identity for device {device_id}: {path}")
                if _VERSION.fullmatch(version) is None or "/" in identity or ".." in identity:
                    raise ValueError(f"unsafe model identity for device {device_id}: {path}")
                target = (
                    template_root / "device_models" / f"device_model_{identity}_v{version}.yaml"
                )
                if target not in documents:
                    raise ValueError(f"missing model target {target} from {path}:{device_id}")


_INSTANCE_PREFIXES = {
    "site": "site",
    "device": "device",
    "collector_tasks": "collector_tasks",
    "scenario": "scenarios",
    "sink": "sinks",
    "delivery": "deliveries",
}
_INSTANCE_ROLES = {
    "site": "site",
    "devices": "device",
    "tasks": "collector_tasks",
    "scenarios": "scenario",
    "sinks": "sink",
    "deliveries": "delivery",
}


def _validate_document_identities(
    root: Path, documents: dict[Path, dict[str, Any]]
) -> None:
    """Enforce publication filenames and immutable identity/version agreement."""
    template_prefixes = {
        "point_tables": ("point_table_", "template_id"),
        "device_models": ("device_model_", "template_id"),
        "protocols": ("protocol_", "template_id"),
        "sinks": ("sink_", "template_id"),
        "deliveries": ("delivery_", "template_id"),
    }
    instance_prefixes = (
        "collector_tasks_", "deliveries_", "scenarios_", "sinks_",
        "device_", "site_",
    )
    for path, document in documents.items():
        name = path.name
        version = document["file_version"]
        if path == root / "services" / f"storage_bootstrap_v{version}.yaml":
            continue  # Local-only Storage bootstrap, excluded from Repository publication.
        expected = f"_v{version}.yaml"
        if not name.endswith(expected):
            raise ValueError(f"configuration filename/file_version mismatch: {path}")
        if path.parent == root / "services":
            prefix, identity_key = "service_", "service_id"
        elif path.parent == root / "deployments":
            prefix, identity_key = "deployment_", "deployment_id"
        elif path.parent == root / "instances":
            prefix = next((item for item in instance_prefixes if name.startswith(item)), "")
            identity_key = ""
        elif path.parent == root / "templates":
            prefix, identity_key = "", ""
            if name not in {
                f"business_points_standard_v{version}.yaml",
                f"units_standard_v{version}.yaml",
                f"observability_v{version}.yaml",
            }:
                raise ValueError(f"unknown standard catalog filename: {path}")
        elif path.parent.parent == root / "templates":
            category = template_prefixes.get(path.parent.name)
            if category is None:
                raise ValueError(f"unknown configuration template category: {path}")
            prefix, identity_key = category
        else:
            raise ValueError(f"unsupported configuration YAML path: {path}")
        if path.parent == root / "templates":
            continue
        if not prefix or not name.startswith(prefix):
            raise ValueError(f"invalid configuration filename prefix: {path}")
        logical_id = name[len(prefix) : -len(expected)]
        if not logical_id or (identity_key and document.get(identity_key) != logical_id):
            raise ValueError(f"configuration filename/identity mismatch: {path}")


def _validate_deployments(root: Path, documents: dict[Path, dict[str, Any]]) -> None:
    """Fail closed on exact Deployment → Service → Instance → Template bindings."""
    for deployment_path, deployment in documents.items():
        if deployment_path.parent != root / "deployments":
            continue
        deployment_id = deployment.get("deployment_id")
        version = deployment["file_version"]
        if (
            not isinstance(deployment_id, str)
            or deployment_path.name != f"deployment_{deployment_id}_v{version}.yaml"
        ):
            raise ValueError(f"Deployment filename/identity mismatch: {deployment_path}")
        services = deployment.get("services")
        instances = deployment.get("instances")
        if not isinstance(services, dict) or not services or not isinstance(instances, dict):
            raise ValueError(f"invalid Deployment selections: {deployment_path}")
        selected_instances: dict[tuple[str, str], dict[str, Any]] = {}
        for kind, entries in instances.items():
            if kind not in _INSTANCE_PREFIXES or not isinstance(entries, dict):
                raise ValueError(f"unsupported Deployment instance type: {kind}")
            for instance_id, selection in entries.items():
                if not isinstance(selection, dict) or set(selection) != {"file_version"}:
                    raise ValueError(f"invalid Deployment instance selection: {kind}/{instance_id}")
                selected_version = selection["file_version"]
                if (
                    not isinstance(selected_version, str)
                    or not _VERSION.fullmatch(selected_version)
                ):
                    raise ValueError(f"invalid Deployment instance version: {kind}/{instance_id}")
                prefix = _INSTANCE_PREFIXES[kind]
                instance_path = (
                    root / "instances" / f"{prefix}_{instance_id}_v{selected_version}.yaml"
                )
                if instance_path not in documents:
                    raise ValueError(f"missing selected Instance: {instance_path}")
                selected_instances[kind, instance_id] = documents[instance_path]
        for service_id, selection in services.items():
            if (
                not isinstance(selection, dict)
                or set(selection) != {"config_id", "file_version"}
                or selection["config_id"] != service_id
            ):
                raise ValueError(f"invalid Deployment Service selection: {service_id}")
            service_version = selection["file_version"]
            if not isinstance(service_version, str) or not _VERSION.fullmatch(service_version):
                raise ValueError(f"invalid Deployment Service version: {service_id}")
            service_path = root / "services" / f"service_{service_id}_v{service_version}.yaml"
            service = documents.get(service_path)
            if service is None or service.get("service_id") != service_id:
                raise ValueError(f"missing or mismatched selected Service: {service_path}")
            sources = service.get("config_sources")
            if sources is None:
                continue
            if not isinstance(sources, dict) or set(sources) != {"instances"}:
                raise ValueError(f"Service must reference Instances only: {service_path}")
            refs = sources["instances"]
            if not isinstance(refs, dict):
                raise ValueError(f"invalid Service Instance references: {service_path}")
            for role, instance_id in refs.items():
                kind = _INSTANCE_ROLES.get(role)
                if kind is None or (kind, instance_id) not in selected_instances:
                    raise ValueError(
                        f"unselected Service Instance: {service_id}/{role}/{instance_id}"
                    )
        for (kind, _instance_id), instance in selected_instances.items():
            if kind not in {"sink", "delivery"}:
                continue
            collection = instance.get("sinks" if kind == "sink" else "deliveries")
            if not isinstance(collection, dict):
                raise ValueError(f"missing {kind} Instance members")
            template_dir = "sinks" if kind == "sink" else "deliveries"
            for member_id, member in collection.items():
                if not isinstance(member, dict):
                    raise ValueError(f"invalid {kind} member: {member_id}")
                template_id = member.get("template_id")
                template_version = member.get("template_version")
                if (
                    not isinstance(template_id, str)
                    or not isinstance(template_version, str)
                    or not _VERSION.fullmatch(template_version)
                ):
                    raise ValueError(f"invalid template binding: {kind}/{member_id}")
                template_path = (
                    root / "templates" / template_dir
                    / f"{kind}_{template_id}_v{template_version}.yaml"
                )
                template = documents.get(template_path)
                if template is None or template.get("template_id") != template_id:
                    raise ValueError(f"missing selected Template: {template_path}")
                if kind == "delivery":
                    sink_ids = {
                        sink_id
                        for (instance_type, _), sink_instance in selected_instances.items()
                        if instance_type == "sink"
                        for sink_id in sink_instance.get("sinks", {})
                    }
                    if member.get("sink_id") not in sink_ids:
                        raise ValueError(f"Delivery references unselected Sink: {member_id}")
                    source = member.get("source")
                    if not isinstance(source, dict):
                        raise ValueError(f"invalid Delivery source: {member_id}")
                    allowed_devices = {
                        device_id
                        for (instance_type, _), devices in selected_instances.items()
                        if instance_type == "device"
                        for device_id in devices.get("devices", {})
                    }
                    if not set(source.get("devices", [])) <= allowed_devices:
                        raise ValueError(f"Delivery references unknown Device: {member_id}")


def _resolve_points(
    path: Path,
    documents: dict[Path, dict[str, Any]],
    cache: dict[Path, dict[str, dict[str, Any]]],
    visiting: set[Path],
) -> dict[str, dict[str, Any]]:
    """Expand exact versioned inheritance before auditing business identity."""
    if path in cache:
        return cache[path]
    if path in visiting:
        raise ValueError(f"PointTable inheritance cycle: {path}")
    if path not in documents:
        raise ValueError(f"unknown PointTable: {path}")
    visiting.add(path)
    document = documents[path]
    raw_points = document.get("points")
    if not isinstance(raw_points, dict):
        raise ValueError(f"PointTable points must be a mapping: {path}")
    points: dict[str, dict[str, Any]] = {}
    parent = document.get("parent")
    if parent is not None:
        if not isinstance(parent, dict) or set(parent) != {"id", "version"}:
            raise ValueError(f"invalid PointTable parent: {path}")
        identity, version = parent["id"], parent["version"]
        if not isinstance(identity, str) or not isinstance(version, str):
            raise ValueError(f"invalid PointTable parent: {path}")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.-]*", identity):
            raise ValueError(f"unsafe PointTable parent identity: {path}")
        if _VERSION.fullmatch(version) is None:
            raise ValueError(f"invalid PointTable parent version: {path}")
        parent_path = path.parent / f"point_table_{identity}_v{version}.yaml"
        if parent_path not in documents:
            raise ValueError(f"missing PointTable parent {parent_path}")
        parent_doc = documents[parent_path]
        if parent_doc.get("protocol") != document.get("protocol"):
            raise ValueError(f"PointTable parent protocol mismatch: {path}")
        points = {
            key: dict(value)
            for key, value in _resolve_points(parent_path, documents, cache, visiting).items()
        }
    remove = document.get("remove_points", [])
    if not isinstance(remove, list) or len(set(remove)) != len(remove):
        raise ValueError(f"invalid PointTable remove_points: {path}")
    for key in remove:
        if not isinstance(key, str) or key not in points:
            raise ValueError(f"cannot remove unknown inherited Point: {path}:{key}")
        del points[key]
    for point_id, patch in raw_points.items():
        if not isinstance(point_id, str) or not isinstance(patch, dict):
            raise ValueError(f"invalid PointTable point: {path}:{point_id}")
        points[point_id] = {**points.get(point_id, {}), **patch}
    visiting.remove(path)
    cache[path] = points
    return points


def audit(root: Path, output: Path) -> int:
    """Write a complete, non-destructive audit; return the number of findings."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"unsafe configuration root: {root}")
    root = root.resolve(strict=True)
    catalogs = sorted((root / "templates").glob("business_points_standard_v*.yaml"))
    unit_catalogs = sorted((root / "templates").glob("units_standard_v*.yaml"))
    if len(catalogs) != 1 or len(unit_catalogs) != 1:
        raise ValueError("audit requires one explicitly selected version of each standard catalog")
    catalog = _load(catalogs[0], root)
    unit_catalog = _load(unit_catalogs[0], root)
    businesses = catalog.get("business_points")
    units = unit_catalog.get("units")
    if not isinstance(businesses, dict) or not isinstance(units, dict):
        raise ValueError("invalid BusinessPoint or Unit catalog")
    documents = sorted(root.rglob("*.yaml"))
    loaded = {path: _load(path, root) for path in documents}
    _validate_document_identities(root, loaded)
    _validate_references(root, loaded)
    _validate_deployments(root, loaded)
    tables = sorted((root / "templates/point_tables").glob("*.yaml"))
    rows: list[dict[str, str]] = []
    cache: dict[Path, dict[str, dict[str, Any]]] = {}
    for path in tables:
        document = loaded[path]
        matched = _TABLE_FILE.fullmatch(path.name)
        if matched is None or document.get("template_id") != matched["id"]:
            raise ValueError(f"PointTable filename/template_id mismatch: {path}")
        if document["file_version"] != matched["version"]:
            raise ValueError(f"PointTable filename/file_version mismatch: {path}")
        points = _resolve_points(path, loaded, cache, set())
        counts = Counter(
            item.get("business_point_id") for item in points.values() if isinstance(item, dict)
        )
        for point_id, item in points.items():
            if not isinstance(item, dict):
                raise ValueError(f"PointTable point must be a mapping: {path}:{point_id}")
            business = item.get("business_point_id")
            unit = item.get("unit")
            issues = []
            if not isinstance(business, str) or not business:
                issues.append("MISSING_EXPLICIT_BUSINESS_POINT")
            elif business not in businesses:
                issues.append("UNKNOWN_BUSINESS_POINT")
            elif counts[business] > 1:
                issues.append("DUPLICATE_BUSINESS_POINT")
            if not isinstance(unit, str) or unit not in units:
                issues.append("UNKNOWN_SOURCE_UNIT")
            address = item.get("address")
            symbol = address.get("symbol", "") if isinstance(address, dict) else ""
            rows.append(
                {
                    "point_table_id": path.stem,
                    "point_id": str(point_id),
                    "business_point_id": str(business or ""),
                    "source_unit": str(unit or ""),
                    "source_data_type": str(item.get("data_type", "")),
                    "plc_symbol": str(symbol),
                    "issues": ";".join(issues),
                    "verification": (
                        "PENDING_FIELD_VERIFICATION" if issues or symbol else "STRUCTURAL_ONLY"
                    ),
                }
            )
    if not rows:
        raise ValueError("no PointTable points found")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.is_symlink():
        raise ValueError(f"unsafe output file: {output}")
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    findings = sum(bool(row["issues"]) for row in rows)
    print(
        f"Checked {len(documents)} YAML files; audited {len(tables)} PointTables / "
        f"{len(rows)} points; {findings} flagged rows"
    )
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("configs"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--strict", action="store_true", help="fail on unresolved issues")
    args = parser.parse_args()
    findings = audit(args.root, args.output)
    if args.strict and findings:
        parser.exit(1, f"BLOCKED: {findings} points require correction or verification\n")


if __name__ == "__main__":
    main()
