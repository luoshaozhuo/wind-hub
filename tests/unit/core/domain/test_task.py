"""Task 领域约束测试。"""

import pytest

from core.domain import DeviceGroup, Task, devices_for_task, validate_task_references
from core.domain.identities import DeviceGroupId


def make_task(**overrides: object) -> Task:
    values = {
        "task_id": "task-1",
        "device_group_id": DeviceGroupId("wind-turbines"),
        "point_group": "fast",
        "sink_ids": ("file", "redis"),
        "interval": 1.0,
    }
    values.update(overrides)
    return Task(**values)


def test_task_uses_device_group_and_multiple_sinks() -> None:
    task = make_task()
    assert task.device_group_id == "wind-turbines"
    assert task.sink_ids == ("file", "redis")
    assert task.interval == 1.0
    assert not hasattr(task, "device_id")


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("task_id", ""),
        ("device_group_id", ""),
        ("point_group", " "),
        ("sink_ids", ()),
        ("sink_ids", ("file", "file")),
        ("sink_ids", ("file", "")),
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


def test_task_copies_sink_ids_to_tuple() -> None:
    sink_ids = ["file", "redis"]
    task = make_task(sink_ids=sink_ids)
    sink_ids.append("other")
    assert task.sink_ids == ("file", "redis")


def test_task_reference_validation() -> None:
    task = make_task()
    groups = {DeviceGroupId("wind-turbines"): DeviceGroup(DeviceGroupId("wind-turbines"), "风机")}
    validate_task_references({task.task_id: task}, groups, {"file", "redis"})

    with pytest.raises(ValueError, match="unknown device group"):
        validate_task_references({"task-1": task}, {}, {"file", "redis"})
    with pytest.raises(ValueError, match="unknown sinks"):
        validate_task_references({"task-1": task}, groups, {"file"})
    with pytest.raises(ValueError, match="does not match task_id"):
        validate_task_references({"wrong": task}, groups, {"file", "redis"})


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
