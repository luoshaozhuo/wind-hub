"""Tests for shared configuration document metadata."""

from __future__ import annotations

import pytest

from core.application.errors import ConfigError
from core.infrastructure.config.document_version import validate_document_version


def test_valid_version() -> None:
    result = validate_document_version(
        {"schema_version": 1, "file_version": "1.2.3"},
        "devices.yaml",
    )
    assert result.schema_version == 1
    assert result.file_version == "1.2.3"


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": 1},
        {"schema_version": 1, "file_version": "latest"},
        {"schema_version": 1, "file_version": 1},
        {"schema_version": True, "file_version": "1.0.0"},
        {"schema_version": 2, "file_version": "1.0.0"},
    ],
)
def test_reject_invalid_versions(document: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        validate_document_version(document, "devices.yaml")
