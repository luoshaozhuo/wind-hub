"""Immutable PointTable closure queries remain valid after recommendation changes."""

from __future__ import annotations

import pytest

from configuration_service.application.service import ConfigurationApplication
from configuration_service.domain.models import (
    RepositoryConflictError,
    RepositoryNotFoundError,
)
from tests.configuration_service.conftest import document_input, publish_documents


def test_historical_point_table_survives_new_recommended_version(
    app: ConfigurationApplication,
) -> None:
    first = document_input(
        "point_table", "wtg", "1.0.0", template_id="wtg",
        protocol="modbus", points={},
    )
    revision, _ = publish_documents(app, (first,), request_id="point-v1")
    assert revision > 0
    first_records, first_manifest = app.get_point_table_bundle("point_table_wtg_v1.0.0")
    assert len(first_records) == 1
    assert first_records[0].identity.config_id == "wtg"
    assert first_records[0].file_version == "1.0.0"
    assert len(first_manifest) == 64

    second = document_input(
        "point_table", "wtg", "2.0.0", template_id="wtg",
        protocol="modbus", points={},
    )
    publish_documents(
        app, (second,), request_id="point-v2", expected_revision=revision
    )
    historical, manifest = app.get_point_table_bundle(
        "point_table_wtg_v1.0.0",
        expected_manifest_sha256=first_manifest,
    )
    assert manifest == first_manifest
    assert historical[0].version_id == first_records[0].version_id


def test_unknown_or_mutated_point_table_identity_fails_closed(
    app: ConfigurationApplication,
) -> None:
    first = document_input(
        "point_table", "wtg", "1.0.0", template_id="wtg",
        protocol="modbus", points={},
    )
    publish_documents(app, (first,), request_id="point-first")
    with pytest.raises(RepositoryNotFoundError, match="PointTable"):
        app.get_point_table_bundle("point_table_wtg_v999.0.0")
    with pytest.raises(ValueError, match="point_table_id"):
        app.get_point_table_bundle("wtg")
    with pytest.raises(RepositoryConflictError, match="manifest hash mismatch"):
        app.get_point_table_bundle(
            "point_table_wtg_v1.0.0", expected_manifest_sha256="0" * 64
        )
