"""Explicitly separate local process bootstrap YAML from Repository publication."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import typer

from cli.configuration_commands import _is_local_bootstrap, publish


@pytest.mark.parametrize(
    "path",
    [
        "services/cli.example.yaml",
        "services/storage_bootstrap_v1.0.0.yaml",
        "services/storage_bootstrap_v12.3.45.yaml",
    ],
)
def test_local_bootstrap_is_not_a_repository_document(path: str) -> None:
    assert _is_local_bootstrap(path)


@pytest.mark.parametrize(
    "path",
    [
        "services/service_storage-main_v1.1.0.yaml",
        "services/service_commander_v1.4.0.yaml",
        "services/storage_bootstrap_v1.0.0.yaml.bak",
        "services/storage_bootstrap_v1.0.yaml",
        "services/other_bootstrap_v1.0.0.yaml",
        "instances/device_farm-a_v1.4.0.yaml",
        "templates/units_standard_v1.2.0.yaml",
        "services/cli.yaml",
    ],
)
def test_unknown_yaml_is_never_silently_excluded(path: str) -> None:
    assert not _is_local_bootstrap(path)


def test_complete_publication_excludes_only_local_bootstraps(tmp_path: Path) -> None:
    services = tmp_path / "services"
    services.mkdir()
    (services / "service_storage-main_v1.1.0.yaml").write_text(
        "schema_version: 2\nfile_version: 1.1.0\nservice_id: storage-main\n",
        encoding="utf-8",
    )
    (services / "storage_bootstrap_v1.0.0.yaml").write_text(
        "not_a_repository_document: true\n", encoding="utf-8"
    )
    (services / "cli.example.yaml").write_text("cli: {}\n", encoding="utf-8")

    def discard_coroutine(_command: str, invocation: Any) -> None:
        invocation.close()

    with patch("cli.main._run", side_effect=discard_coroutine) as run:
        publish(root=tmp_path)
    run.assert_called_once()

    (services / "unknown_bootstrap_v1.0.0.yaml").write_text(
        "not_a_repository_document: true\n", encoding="utf-8"
    )
    with pytest.raises(typer.BadParameter, match="invalid configuration"):
        publish(root=tmp_path)
