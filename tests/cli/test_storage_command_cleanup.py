"""Storage CLI has no obsolete per-point or narrow-row command aliases."""

from cli.storage_commands import storage


def test_storage_cli_exposes_only_wide_history_operations() -> None:
    commands = {command.name for command in storage.registered_commands}
    assert {"wide-rows", "wide-aggregates", "wide-export"}.issubset(commands)
    assert not {"samples", "aggregates", "export"} & commands


def test_storage_cli_has_no_legacy_filter_adapter() -> None:
    from cli import storage_commands

    assert not hasattr(storage_commands, "_filter")
    assert not hasattr(storage_commands, "query_samples")
    assert not hasattr(storage_commands, "query_aggregates")
    assert not hasattr(storage_commands, "create_export")
