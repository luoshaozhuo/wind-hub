"""CLI configuration commands expose one full-root publication interface."""

from __future__ import annotations

from cli.main import app, config_app
from core.configuration.publication_identity import (
    resolve_publication_identity,
    validate_relative_path,
)


def test_repository_publish_and_service_snapshot_commands_are_registered() -> None:
    available = {command.name for command in config_app.registered_commands if command.name}
    assert {"publish", "revision", "point-table-bundle", "get", "bundle", "diff"} <= available
    assert "candidate" not in available
    assert {"apply", "push"} <= available
    groups = {group.name for group in app.registered_groups}
    assert "config" in groups
    assert "configuration" not in groups


def test_publication_identity_is_shared_with_configuration_service() -> None:
    path = "templates/point_tables/point_table_modbus_wtg_sim_v1.0.0.yaml"
    validate_relative_path(path)
    identity = resolve_publication_identity(
        path, {"file_version": "1.0.0", "template_id": "modbus_wtg_sim"}
    )
    assert (identity.config_type, identity.config_id, identity.file_version) == (
        "point_table",
        "modbus_wtg_sim",
        "1.0.0",
    )
