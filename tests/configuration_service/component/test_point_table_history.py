"""Historical PointTable queries require immutable BusinessPoint and Units closure."""

from __future__ import annotations

from pathlib import Path

import pytest

from configuration_service.application.service import ConfigurationApplication, DocumentInput
from configuration_service.domain.models import (
    ConfigIdentity,
    RepositoryConflictError,
    RepositoryNotFoundError,
)
from tests.configuration_service.conftest import document_input, publish_documents

ROOT = Path(__file__).resolve().parents[3]


def _table(version: str, *, with_dependencies: bool) -> DocumentInput:
    fields: dict[str, object] = {
        "template_id": "wtg",
        "device_type": "turbine",
        "protocol": "modbus",
        "points": {},
    }
    if with_dependencies:
        fields["config_sources"] = [
            {
                "config_type": "units",
                "config_id": "standard",
                "file_version": "1.1.0",
            },
            {
                "config_type": "business_points",
                "config_id": "standard",
                "file_version": "1.3.0",
            },
        ]
    return document_input("point_table", "wtg", version, **fields)


def _standards() -> tuple[DocumentInput, ...]:
    return (
        DocumentInput(
            identity=ConfigIdentity("units", "standard"),
            file_name="units_standard.yaml",
            yaml_bytes=(ROOT / "configs/templates/units_standard.yaml").read_bytes(),
        ),
        DocumentInput(
            identity=ConfigIdentity("business_points", "standard"),
            file_name="business_points_standard.yaml",
            yaml_bytes=(ROOT / "configs/templates/business_points_standard.yaml").read_bytes(),
        ),
    )


def test_historical_point_table_survives_new_recommended_version(
    app: ConfigurationApplication,
) -> None:
    revision, _ = publish_documents(
        app, (*_standards(), _table("1.0.0", with_dependencies=True)),
        request_id="point-v1",
    )
    assert revision > 0
    first_records, first_manifest = app.get_point_table_bundle("point_table_wtg_v1.0.0")
    assert {item.identity.config_type for item in first_records} == {
        "units", "business_points", "point_table"
    }
    assert len(first_manifest) == 64

    publish_documents(
        app,
        (_table("2.0.0", with_dependencies=True),),
        request_id="point-v2",
        expected_revision=revision,
    )
    historical, manifest = app.get_point_table_bundle(
        "point_table_wtg_v1.0.0",
        expected_manifest_sha256=first_manifest,
    )
    assert manifest == first_manifest
    assert historical[-1].version_id == first_records[-1].version_id


def test_incomplete_point_table_closure_rejected(app: ConfigurationApplication) -> None:
    publish_documents(
        app, (_table("1.0.0", with_dependencies=False),), request_id="incomplete"
    )
    with pytest.raises(RepositoryConflictError, match="BusinessPoint/Units"):
        app.get_point_table_bundle("point_table_wtg_v1.0.0")


def test_unknown_or_mutated_point_table_identity_fails_closed(
    app: ConfigurationApplication,
) -> None:
    publish_documents(
        app, (*_standards(), _table("1.0.0", with_dependencies=True)),
        request_id="first",
    )
    with pytest.raises(RepositoryNotFoundError, match="PointTable"):
        app.get_point_table_bundle("point_table_wtg_v999.0.0")
    with pytest.raises(ValueError, match="point_table_id"):
        app.get_point_table_bundle("wtg")
    with pytest.raises(RepositoryConflictError, match="manifest hash mismatch"):
        app.get_point_table_bundle(
            "point_table_wtg_v1.0.0", expected_manifest_sha256="0" * 64
        )
