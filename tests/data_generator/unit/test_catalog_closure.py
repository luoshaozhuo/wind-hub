"""Version-pinned Generator catalogs must come from Core's verified source closure."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.configuration.document_version import SourceIdentity
from core.configuration.shared_documents import SharedDocuments
from data_generator.domain.model import GeneratorError
from data_generator.infrastructure.config.service_settings import _loaded_catalog

pytestmark = pytest.mark.unit


def _documents(*paths: Path) -> SharedDocuments:
    sources = tuple(
        SourceIdentity(
            path=path,
            sha256="0" * 64,
            schema_version=1,
            file_version=path.stem.rsplit("_v", maxsplit=1)[-1],
        )
        for path in paths
    )
    return SharedDocuments(
        site={},
        devices={},
        units={},
        business_points={},
        revision="test",
        devices_file_version="1.0.0",
        source_manifest=sources,
    )


def test_resolves_pinned_business_catalog_from_actual_loaded_identity(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    catalog = templates / "business_points_standard_v1.5.0.yaml"
    documents = _documents(catalog)
    assert _loaded_catalog(documents, templates, "business_points_standard") == catalog


def test_rejects_missing_and_ambiguous_catalogs(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    first = templates / "business_points_standard_v1.4.0.yaml"
    second = templates / "business_points_standard_v1.5.0.yaml"
    with pytest.raises(GeneratorError, match="exactly one"):
        _loaded_catalog(_documents(), templates, "business_points_standard")
    with pytest.raises(GeneratorError, match="exactly one"):
        _loaded_catalog(_documents(first, second), templates, "business_points_standard")
