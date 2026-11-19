"""Parse and authenticate Configuration Repository historical YAML closures."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from contracts.configuration_repository_pb2 import GetPointTableBundleResponse
from core.application.errors import ConfigError
from core.configuration.business_points import parse_business_points
from core.configuration.shared_point_tables import _point_tables
from core.configuration.units import parse_unit_catalog
from core.domain import PointTableId
from core.infrastructure.config.generic_yaml import ConfigOperationError, parse_yaml
from sink_service.application.business_projection import HistoricalPointTable
from sink_service.domain.config import ConfigurationError

_MAX_BUNDLE_BYTES = 4 * 1024 * 1024
_MAX_DOCUMENTS = 128
_POINT_TABLE_ID = re.compile(r"point_table_([A-Za-z0-9_.-]+)_v([0-9]+\.[0-9]+\.[0-9]+)")


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def parse_point_table_bundle(
    response: GetPointTableBundleResponse, *, expected_id: str, expected_type: str
) -> HistoricalPointTable:
    """Verify transport hashes, manifest, exact identity and complete pinned schema."""
    if response.point_table_id != expected_id or _POINT_TABLE_ID.fullmatch(expected_id) is None:
        raise ConfigurationError("PointTable response identity mismatch")
    if not response.device_type or response.device_type != expected_type:
        raise ConfigurationError("PointTable device_type does not match source")
    if not 0 < len(response.documents) <= _MAX_DOCUMENTS:
        raise ConfigurationError("invalid historical bundle document count")
    if sum(len(item.yaml_bytes) for item in response.documents) > _MAX_BUNDLE_BYTES:
        raise ConfigurationError("historical bundle capacity exceeded")

    manifest: list[dict[str, str]] = []
    documents: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in response.documents:
        identity = item.config_id
        config_type, config_id, version = (
            identity.config_type, identity.config_id, item.file_version
        )
        key = (config_type, config_id, version)
        if not all(key) or key in documents or not item.version_id or not item.relative_path:
            raise ConfigurationError("duplicate or incomplete historical version")
        digest = hashlib.sha256(item.yaml_bytes).hexdigest()
        if digest != item.yaml_sha256:
            raise ConfigurationError("historical YAML checksum mismatch")
        try:
            decoded = parse_yaml(item.yaml_bytes.decode("utf-8", errors="strict"))
        except (UnicodeError, ConfigOperationError) as exc:
            raise ConfigurationError("invalid historical YAML") from exc
        if (
            decoded.get("file_version") != version
            or decoded.get("schema_version") != item.schema_version
        ):
            raise ConfigurationError("historical YAML header mismatch")
        if hashlib.sha256(_canonical(decoded)).hexdigest() != item.content_sha256:
            raise ConfigurationError("historical canonical hash mismatch")
        if config_type == "point_table" and decoded.get("template_id") != config_id:
            raise ConfigurationError("historical PointTable template identity mismatch")
        if config_type not in {"point_table", "business_points", "units"}:
            raise ConfigurationError("unexpected document in PointTable closure")
        documents[key] = decoded
        manifest.append({
            "config_type": config_type,
            "config_id": config_id,
            "version_id": item.version_id,
            "content_sha256": item.content_sha256,
            "yaml_sha256": item.yaml_sha256,
            "relative_path": item.relative_path,
        })
    manifest.sort(key=lambda x: (x["config_type"], x["config_id"], x["version_id"]))
    manifest_hash = hashlib.sha256(_canonical(manifest)).hexdigest()
    if not response.manifest_sha256 or manifest_hash != response.manifest_sha256:
        raise ConfigurationError("historical PointTable manifest mismatch")

    matches = _POINT_TABLE_ID.fullmatch(expected_id)
    assert matches is not None
    table_key = ("point_table", matches.group(1), matches.group(2))
    if table_key not in documents:
        raise ConfigurationError("requested historical PointTable is absent")
    unit_docs = [doc for key, doc in documents.items() if key[0] == "units"]
    business_docs = [doc for key, doc in documents.items() if key[0] == "business_points"]
    if len(unit_docs) != 1 or len(business_docs) != 1:
        raise ConfigurationError("ambiguous pinned Units/BusinessPoint versions")
    try:
        units = parse_unit_catalog({
            key: value for key, value in unit_docs[0].items()
            if key not in {"schema_version", "file_version"}
        })
        business_points = parse_business_points(
            {"business_points": business_docs[0]["business_points"]}, units
        )
        definitions: dict[str, dict[str, Any]] = {}
        for (kind, config_id, version), doc in documents.items():
            if kind != "point_table":
                continue
            table_id = f"point_table_{config_id}_v{version}"
            source = {
                "protocol": doc.get("protocol"),
                "points": doc.get("points", {}),
                "remove_points": doc.get("remove_points", []),
            }
            parent = doc.get("parent")
            if parent is not None:
                if not isinstance(parent, dict) or set(parent) != {"id", "version"}:
                    raise ConfigurationError("invalid historical parent reference")
                source["parent_id"] = f"point_table_{parent['id']}_v{parent['version']}"
            definitions[table_id] = source
        tables = _point_tables({"point_tables": definitions}, business_points, units)
    except (ConfigError, KeyError, ValueError, TypeError) as exc:
        raise ConfigurationError("invalid pinned PointTable interpretation closure") from exc
    root = tables.get(PointTableId(expected_id))
    if root is None:
        raise ConfigurationError("unable to resolve immutable PointTable")
    raw_root = documents[table_key]
    declared_type = raw_root.get("device_type")
    if declared_type is not None and declared_type != expected_type:
        raise ConfigurationError("historical PointTable device type declaration mismatch")
    return HistoricalPointTable(
        point_table_id=expected_id,
        device_type=expected_type,
        manifest_sha256=manifest_hash,
        table=root,
        business_points=business_points,
    )
