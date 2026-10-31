"""Service-independent configuration loader contracts."""

from __future__ import annotations

from pathlib import Path
from shutil import copytree

import pytest

from core.application.errors import ConfigError
from core.infrastructure.config.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
)

_CONFIGS = Path(__file__).resolve().parents[4] / "configs"


def test_shared_loader_resolves_devices_without_service_config() -> None:
    loader = SharedDocumentLoader(
        SharedConfigPaths(
            instances=_CONFIGS / "instances",
            templates=_CONFIGS / "templates",
        )
    )

    documents = loader.load()

    assert documents.site["site_id"] == "example_site"
    assert "WT01" in documents.devices
    assert documents.devices["WT01"].templates.protocol.document["protocol"] == "modbus"
    assert len(documents.revision) == 64


def test_shared_loader_requires_instances(tmp_path: Path) -> None:
    loader = SharedDocumentLoader(
        SharedConfigPaths(
            instances=tmp_path,
            templates=_CONFIGS / "templates",
        )
    )
    with pytest.raises(ConfigError, match="not found"):
        loader.load()


def test_shared_loader_rejects_invalid_file_version(tmp_path: Path) -> None:
    instances = tmp_path / "instances"
    copytree(_CONFIGS / "instances", instances)
    site = instances / "site.yaml"
    site.write_text(
        site.read_text(encoding="utf-8").replace("file_version: 1.0.0", "file_version: latest"),
        encoding="utf-8",
    )
    loader = SharedDocumentLoader(
        SharedConfigPaths(instances=instances, templates=_CONFIGS / "templates")
    )
    with pytest.raises(ConfigError, match="file_version"):
        loader.load()
