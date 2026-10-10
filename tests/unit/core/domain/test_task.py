"""Task 领域约束测试。"""

import pytest

from core.domain import Task
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
