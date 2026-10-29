"""Task 领域约束测试。

Sink Service 独立服务化后，Task 只描述「在哪个设备组上以多大节拍采哪个
测点组」；输出去向（Sink 绑定）由 sink_service 的 sinks.yaml 声明，Task
不再持有 sink_ids。
"""

from typing import Any

import pytest

from core.domain import (
    DeviceGroup,
    Task,
    devices_for_task,
    validate_core_config,
    validate_task_references,
)
from core.domain.identities import DeviceGroupId
from tests.support.units import minimal_unit_catalog


def make_task(**overrides: object) -> Task:
    values: dict[str, Any] = {
        "task_id": "task-1",
        "device_group_id": DeviceGroupId("wind-turbines"),
        "point_group": "fast",
        "interval": 1.0,
    }
    values.update(overrides)
    return Task(**values)


def test_task_uses_device_group_and_interval() -> None:
    task = make_task()
    assert task.device_group_id == "wind-turbines"
    assert task.point_group == "fast"
    assert task.interval == 1.0
    assert task.enabled is True
    assert not hasattr(task, "device_id")
    assert not hasattr(task, "sink_ids")


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("task_id", ""),
        ("device_group_id", ""),
        ("point_group", " "),
        ("interval", 0),
        ("interval", -1),
        ("interval", float("inf")),
        ("interval", float("nan")),
        ("interval", True),
        ("enabled", 1),
    ],
)
def test_task_rejects_invalid_values(field: str, invalid: object) -> None:
    with pytest.raises(ValueError):
        make_task(**{field: invalid})


def test_task_strips_identity_fields_and_float_interval() -> None:
    task = make_task(task_id=" task-1 ", device_group_id=" wind-turbines ", interval=2)
    assert task.task_id == "task-1"
    assert task.device_group_id == "wind-turbines"
    assert task.interval == 2.0


def test_task_reference_validation() -> None:
    task = make_task()
    groups = {DeviceGroupId("wind-turbines"): DeviceGroup(DeviceGroupId("wind-turbines"), "风机")}
    validate_task_references({task.task_id: task}, groups)

    with pytest.raises(ValueError, match="unknown device group"):
        validate_task_references({"task-1": task}, {})
    with pytest.raises(ValueError, match="does not match task_id"):
        validate_task_references({"wrong": task}, groups)


def test_task_device_selection_supports_multiple_groups() -> None:
    from core.domain import ConnectionEndpoint, Device
    from core.domain.identities import DeviceId, DeviceModelId

    def device(device_id: str, *groups: str) -> Device:
        return Device(
            device_id=DeviceId(device_id),
            device_model_id=DeviceModelId("model"),
            endpoint=ConnectionEndpoint(host="127.0.0.1", port=502),
            device_group_ids=tuple(DeviceGroupId(group) for group in groups),
        )

    devices = {
        DeviceId("d1"): device("d1", "wind-turbines", "all"),
        DeviceId("d2"): device("d2", "other"),
    }
    assert tuple(item.device_id for item in devices_for_task(make_task(), devices)) == ("d1",)


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
        "units": minimal_unit_catalog(),
        "tasks": {task.task_id: task},
    }
    validate_core_config(**args)
