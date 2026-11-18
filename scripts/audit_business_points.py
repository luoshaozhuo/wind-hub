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


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_mapping
)

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
    """Verify exact model, PointTable, protocol and task references."""
    template_root = root / "templates"
    for path, document in documents.items():
        if path.parent == template_root / "device_models":
            for category, prefix in (
                ("protocol", "protocol"),
                ("point_table", "point_table"),
                ("task", "task"),
            ):
                reference = document.get(category)
                if reference is None and category == "task":
                    continue
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
        if path.parent == root / "instances" and path.name.startswith("devices"):
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
                target = template_root / "device_models" / f"device_model_{identity}_v{version}.yaml"
                if target not in documents:
                    raise ValueError(f"missing model target {target} from {path}:{device_id}")


def audit(root: Path, output: Path) -> int:
    """Write a complete, non-destructive audit; return the number of findings."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"unsafe configuration root: {root}")
    root = root.resolve(strict=True)
    catalog = _load(root / "templates/business_points_standard.yaml", root)
    unit_catalog = _load(root / "templates/units_standard.yaml", root)
    businesses = catalog.get("business_points")
    units = unit_catalog.get("units")
    if not isinstance(businesses, dict) or not isinstance(units, dict):
        raise ValueError("invalid BusinessPoint or Unit catalog")
    documents = sorted(root.rglob("*.yaml"))
    for path in documents:
        _load(path, root)
    _validate_references(root, {path: _load(path, root) for path in documents})
    tables = sorted((root / "templates/point_tables").glob("*.yaml"))
    rows: list[dict[str, str]] = []
    for path in tables:
        document = _load(path, root)
        matched = _TABLE_FILE.fullmatch(path.name)
        if matched is None or document.get("template_id") != matched["id"]:
            raise ValueError(f"PointTable filename/template_id mismatch: {path}")
        if document["file_version"] != matched["version"]:
            raise ValueError(f"PointTable filename/file_version mismatch: {path}")
        points = document.get("points")
        if not isinstance(points, dict):
            raise ValueError(f"PointTable points must be a mapping: {path}")
        counts = Counter(
            item.get("business_point_id")
            for item in points.values()
            if isinstance(item, dict)
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
                        "PENDING_FIELD_VERIFICATION"
                        if issues or symbol
                        else "STRUCTURAL_ONLY"
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
