"""Fail-closed tests for the historical PointTable bundle codec.

Verifies transport YAML hashes, canonical semantic hashes and the manifest
digest, plus identity/device-type matching, duplicate rejection and missing
dependency rejection. Every malformed bundle must raise rather than degrade.
"""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from contracts.configuration_repository_pb2 import (
    ConfigId,
    ConfigYamlDocument,
    GetPointTableBundleResponse,
)
from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.adapters.outbound.historical_bundle_codec import (
    parse_point_table_bundle,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

POINT_TABLE_ID = "point_table_modbus_pv_sim_v1.0.0"
DEVICE_TYPE = "photovoltaic"

UNITS = {
    "schema_version": 1,
    "file_version": "1.0.0",
    "quantities": {"temperature": {"base_unit": "celsius"}},
    "units": {"celsius": {"symbol": "°C", "quantity": "temperature"}},
}
BUSINESS = {
    "schema_version": 1,
    "file_version": "1.0.0",
    "business_points": {
        "pv_cell_temperature": {
            "data_type": "float32",
            "unit": "celsius",
            "description": "cell temperature",
        }
    },
}
POINT_TABLE = {
    "schema_version": 1,
    "template_id": "modbus_pv_sim",
    "file_version": "1.0.0",
    "protocol": "modbus",
    "device_type": DEVICE_TYPE,
    "points": {
        "cell_temperature": {
            "business_point_id": "pv_cell_temperature",
            "data_type": "float32",
            "unit": "celsius",
            "scale": 1.0,
            "offset": 0.0,
            "address": {
                "register_type": "input",
                "address": 0,
                "count": 2,
                "slave_id": 1,
            },
        }
    },
}


def _canonical(document: dict) -> bytes:
    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _doc(
    config_type: str,
    config_id: str,
    version: str,
    document: dict,
    *,
    relative_path: str,
) -> ConfigYamlDocument:
    yaml_bytes = json.dumps(document, sort_keys=True).encode("utf-8")
    return ConfigYamlDocument(
        config_id=ConfigId(config_type=config_type, config_id=config_id),
        version_id=f"ver-{config_id}",
        schema_version=1,
        file_version=version,
        yaml_bytes=yaml_bytes,
        content_sha256=hashlib.sha256(_canonical(document)).hexdigest(),
        yaml_sha256=hashlib.sha256(yaml_bytes).hexdigest(),
        relative_path=relative_path,
    )


def _bundle_documents() -> list[ConfigYamlDocument]:
    return [
        _doc(
            "units",
            "standard",
            "1.0.0",
            UNITS,
            relative_path="configs/templates/units_standard.yaml",
        ),
        _doc(
            "business_points",
            "standard",
            "1.0.0",
            BUSINESS,
            relative_path="configs/templates/business_points_standard.yaml",
        ),
        _doc(
            "point_table",
            "modbus_pv_sim",
            "1.0.0",
            POINT_TABLE,
            relative_path="configs/templates/point_tables/point_table_modbus_pv_sim_v1.0.0.yaml",
        ),
    ]


def _manifest_sha256(documents: list[ConfigYamlDocument]) -> str:
    manifest = [
        {
            "config_type": item.config_id.config_type,
            "config_id": item.config_id.config_id,
            "version_id": item.version_id,
            "content_sha256": item.content_sha256,
            "yaml_sha256": item.yaml_sha256,
            "relative_path": item.relative_path,
        }
        for item in documents
    ]
    manifest.sort(key=lambda entry: (entry["config_type"], entry["config_id"], entry["version_id"]))
    return hashlib.sha256(_canonical(manifest)).hexdigest()


def _response(documents: list[ConfigYamlDocument] | None = None) -> GetPointTableBundleResponse:
    documents = _bundle_documents() if documents is None else documents
    return GetPointTableBundleResponse(
        point_table_id=POINT_TABLE_ID,
        device_type=DEVICE_TYPE,
        documents=documents,
        manifest_sha256=_manifest_sha256(documents),
    )


def test_valid_closure_parses_into_historical_point_table() -> None:
    result = parse_point_table_bundle(
        _response(), expected_id=POINT_TABLE_ID, expected_type=DEVICE_TYPE
    )
    assert result.point_table_id == POINT_TABLE_ID
    assert result.device_type == DEVICE_TYPE
    assert result.manifest_sha256 == _response().manifest_sha256
    # The pinned PointTable exposes the mapped source point.
    assert "cell_temperature" in result.table.points
    assert "pv_cell_temperature" in result.business_points


def test_response_identity_mismatch_is_rejected() -> None:
    response = _response()
    with pytest.raises(ConfigurationError, match="identity mismatch"):
        parse_point_table_bundle(
            response, expected_id="point_table_other_v1.0.0", expected_type=DEVICE_TYPE
        )


def test_device_type_mismatch_is_rejected() -> None:
    response = _response()
    with pytest.raises(ConfigurationError, match="device_type"):
        parse_point_table_bundle(response, expected_id=POINT_TABLE_ID, expected_type="wind_turbine")


def test_tampered_yaml_fails_transport_checksum() -> None:
    documents = _bundle_documents()
    documents[2].yaml_bytes = documents[2].yaml_bytes + b" "
    response = GetPointTableBundleResponse(
        point_table_id=POINT_TABLE_ID,
        device_type=DEVICE_TYPE,
        documents=documents,
        manifest_sha256=_manifest_sha256(documents),
    )
    with pytest.raises(ConfigurationError, match="checksum"):
        parse_point_table_bundle(response, expected_id=POINT_TABLE_ID, expected_type=DEVICE_TYPE)


def test_tampered_semantic_content_fails_canonical_hash() -> None:
    documents = _bundle_documents()
    # Keep the YAML checksum internally consistent but change the semantic hash.
    documents[2].content_sha256 = "f" * 64
    response = GetPointTableBundleResponse(
        point_table_id=POINT_TABLE_ID,
        device_type=DEVICE_TYPE,
        documents=documents,
        manifest_sha256=_manifest_sha256(documents),
    )
    with pytest.raises(ConfigurationError, match="canonical hash"):
        parse_point_table_bundle(response, expected_id=POINT_TABLE_ID, expected_type=DEVICE_TYPE)


def test_manifest_digest_mismatch_is_rejected() -> None:
    response = _response()
    response.manifest_sha256 = "0" * 64
    with pytest.raises(ConfigurationError, match="manifest mismatch"):
        parse_point_table_bundle(response, expected_id=POINT_TABLE_ID, expected_type=DEVICE_TYPE)


def test_missing_units_dependency_is_rejected() -> None:
    documents = [doc for doc in _bundle_documents() if doc.config_id.config_type != "units"]
    response = GetPointTableBundleResponse(
        point_table_id=POINT_TABLE_ID,
        device_type=DEVICE_TYPE,
        documents=documents,
        manifest_sha256=_manifest_sha256(documents),
    )
    with pytest.raises(ConfigurationError):
        parse_point_table_bundle(response, expected_id=POINT_TABLE_ID, expected_type=DEVICE_TYPE)


def test_duplicate_document_version_is_rejected() -> None:
    documents = _bundle_documents()
    duplicate = copy.deepcopy(documents[0])
    documents.append(duplicate)
    response = GetPointTableBundleResponse(
        point_table_id=POINT_TABLE_ID,
        device_type=DEVICE_TYPE,
        documents=documents,
        manifest_sha256=_manifest_sha256(documents),
    )
    with pytest.raises(ConfigurationError, match="duplicate"):
        parse_point_table_bundle(response, expected_id=POINT_TABLE_ID, expected_type=DEVICE_TYPE)
