"""Service-independent configuration loader contracts."""

from __future__ import annotations

from pathlib import Path
from shutil import copytree

import pytest

from configuration.shared_documents import (
    SharedConfigPaths,
    SharedDocumentLoader,
)
from core.application.errors import ConfigError

_CONFIGS = Path(__file__).resolve().parents[4] / "configs"


def _paths() -> SharedConfigPaths:
    return SharedConfigPaths(
        instances=_CONFIGS / "instances",
        templates=_CONFIGS / "templates",
        site_file=_CONFIGS / "instances" / "site.yaml",
        devices_file=_CONFIGS / "instances" / "devices.yaml",
    )


def test_shared_loader_resolves_devices_without_service_config() -> None:
    loader = SharedDocumentLoader(_paths())

    documents = loader.load()

    assert documents.site["site_id"] == "example_site"
    assert set(documents.devices) == {f"GW0{i}" for i in range(1, 6)} | {
        f"TZ0{i}" for i in range(1, 6)
    }
    assert documents.devices["GW01"].templates.protocol.document["protocol"] == "ads"
    assert documents.devices["TZ01"].templates.protocol.document["protocol"] == "modbus"
    assert len(documents.revision) == 64


def test_shared_loader_requires_explicit_devices_and_site_files(tmp_path: Path) -> None:
    # S2-10：load() 不再回退隐式默认路径，缺显式 devices_file/site_file 必须拒绝。
    loader = SharedDocumentLoader(
        SharedConfigPaths(instances=tmp_path, templates=_CONFIGS / "templates")
    )
    with pytest.raises(ConfigError, match="devices_file must be provided explicitly"):
        loader.load()

    site_only = SharedConfigPaths(
        instances=tmp_path,
        templates=_CONFIGS / "templates",
        site_file=tmp_path / "site.yaml",
    )
    with pytest.raises(ConfigError, match="devices_file must be provided explicitly"):
        SharedDocumentLoader(site_only).load()

    with pytest.raises(ConfigError, match="site_file must be provided when load_site=True"):
        SharedDocumentLoader(
            SharedConfigPaths(
                instances=tmp_path,
                templates=_CONFIGS / "templates",
                devices_file=tmp_path / "devices.yaml",
            )
        ).load()

    # 显式给出但文件不存在时，仍按 REFERENCE_NOT_FOUND 分类而非静默回退。
    missing = SharedConfigPaths(
        instances=tmp_path,
        templates=_CONFIGS / "templates",
        load_site=False,
        devices_file=tmp_path / "missing-devices.yaml",
    )
    with pytest.raises(ConfigError, match="REFERENCE_NOT_FOUND"):
        SharedDocumentLoader(missing).load()


def test_shared_loader_rejects_invalid_file_version(tmp_path: Path) -> None:
    instances = tmp_path / "instances"
    copytree(_CONFIGS / "instances", instances)
    site = instances / "site.yaml"
    site.write_text(
        site.read_text(encoding="utf-8").replace("file_version: 1.0.0", "file_version: latest"),
        encoding="utf-8",
    )
    loader = SharedDocumentLoader(
        SharedConfigPaths(
            instances=instances,
            templates=_CONFIGS / "templates",
            site_file=site,
            devices_file=instances / "devices.yaml",
        )
    )
    with pytest.raises(ConfigError, match="file_version"):
        loader.load()
