"""Task 领域约束测试。"""

from typing import Any

import pytest

from core.domain import (
    DeviceGroup,
    Task,
    validate_core_config,
)
from core.domain.identities import DeviceGroupId


def make_task(**overrides: object) -> Task:
    values: dict[str, Any] = {
        "task_id": "task-1",
        "device_group_id": DeviceGroupId("wind-turbines"),
        "point_group": "fast",
        "sink_ids": ("file", "redis"),
        "interval": 1.0,
    }
    values.update(overrides)
    return Task(**values)


def test_central_domain_validation_checks_tasks() -> None:
    task = make_task()
    group = DeviceGroup(DeviceGroupId("wind-turbines"), "Wind Turbines")
    args: dict[str, Any] = {
        "device_types": {},
        "device_models": {},
        "device_groups": {group.device_group_id: group},
        "devices": {},
        "business_points": {},
        "point_tables": {},
        "protocol_options_by_device": {},
        "tasks": {task.task_id: task},
    }
    validate_core_config(**args, sink_ids={"file", "redis"})
    with pytest.raises(ValueError, match="unknown sinks"):
        validate_core_config(**args, sink_ids={"file"})
