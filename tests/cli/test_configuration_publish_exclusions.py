"""Explicitly separate local process bootstrap YAML from Repository publication."""

from __future__ import annotations

import pytest

from cli.configuration_commands import _is_local_bootstrap


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
