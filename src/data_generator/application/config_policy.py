"""Domain-level service revision and immutable Scenario policies."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from data_generator.domain.model import GeneratorError


def validate_scenario_versions(previous: Mapping[str, Any], proposed: Mapping[str, Any]) -> None:
    existing = {item["scenario_id"]: item for item in previous["scenarios"]}
    for item in proposed["scenarios"]:
        before = existing.get(item["scenario_id"])
        if before is not None and before["version"] == item["version"] and before != item:
            raise GeneratorError(
                "immutable scenario version changed: " f"{item['scenario_id']}@{item['version']}"
            )


def requires_service_restart(previous: Mapping[str, Any], proposed: Mapping[str, Any]) -> bool:
    """Only Scenario changes can hot-swap; all process options require restart."""
    previous_options = {key: value for key, value in previous.items() if key != "scenarios"}
    proposed_options = {key: value for key, value in proposed.items() if key != "scenarios"}
    return previous_options != proposed_options
