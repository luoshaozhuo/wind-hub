"""Full-directory candidate preparation, separate from SQLite publication."""

from __future__ import annotations

import pytest

from configuration_service.application.directory_manifest import DirectoryFile
from configuration_service.application.service import ConfigurationApplication


def test_prepares_sorted_directory_with_pinned_file_versions(
    app: ConfigurationApplication,
) -> None:
    site = DirectoryFile(
        "instances/site.yaml",
        b"schema_version: 1\nfile_version: 1.0.0\nsite_id: site-a\n",
    )
    table = DirectoryFile(
        "templates/point_tables/point_table_wtg_v1.0.0.yaml",
        b"schema_version: 1\nfile_version: 1.0.0\n"
        b"template_id: wtg\nprotocol: modbus\npoints: {}\n",
    )
    manifest, prepared = app.prepare_directory((table, site))
    assert tuple(item.relative_path for item in manifest.files) == (
        "instances/site.yaml",
        "templates/point_tables/point_table_wtg_v1.0.0.yaml",
    )
    assert [(item.identity.config_type, item.identity.config_id) for item in prepared] == [
        ("site", "site"),
        ("point_table", "wtg"),
    ]
    assert manifest.total_bytes == len(site.yaml_bytes) + len(table.yaml_bytes)


def test_rejects_duplicate_logical_file_version(
    app: ConfigurationApplication,
) -> None:
    instance = DirectoryFile(
        "instances/sinks.yaml",
        b"schema_version: 1\nfile_version: 1.0.0\nsinks: {}\n",
    )
    template = DirectoryFile(
        "templates/sinks/sink_sinks_v1.0.0.yaml",
        b"schema_version: 1\nfile_version: 1.0.0\n"
        b"template_id: sinks\nkind: redis\n",
    )
    with pytest.raises(ValueError, match="duplicate configuration document identity"):
        app.prepare_directory((instance, template))


def test_rejects_configuration_service_bootstrap_in_full_root(
    app: ConfigurationApplication,
) -> None:
    bootstrap = DirectoryFile(
        "services/service_configuration_v1.0.0.yaml",
        b"schema_version: 1\nfile_version: 1.0.0\n"
        b"service_id: configuration-main\n",
    )
    with pytest.raises(ValueError, match="bootstrap"):
        app.prepare_directory((bootstrap,))
