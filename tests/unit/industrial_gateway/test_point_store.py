"""共享点位与命令写事件的最小契约测试。"""

import pytest

from industrial_gateway.domain import PointDefinition, PointRole, PointWrite
from industrial_gateway.application.point_store import PointStore


async def test_repeated_command_write_produces_two_events() -> None:
    events: list[PointWrite] = []

    async def accept(request: PointWrite) -> None:
        events.append(request)

    store = PointStore(
        [PointDefinition("t1", "setpoint", PointRole.COMMAND, writable=True)],
        command_handler=accept,
    )
    request = PointWrite("t1", "setpoint", 100.0, "dc-ems")
    await store.write(request)
    await store.write(request)
    assert events == [request, request]
    assert store.read("t1", "setpoint").version == 2


async def test_feedback_refresh_does_not_dispatch_command() -> None:
    store = PointStore([PointDefinition("t1", "power", PointRole.FEEDBACK)])
    await store.update_feedback("t1", "power", 12.5)
    assert store.read("t1", "power").value == 12.5
    with pytest.raises(PermissionError):
        await store.write(PointWrite("t1", "power", 1.0, "dc-ems"))


async def test_command_without_handler_is_rejected() -> None:
    store = PointStore([PointDefinition("t1", "setpoint", PointRole.COMMAND, writable=True)])
    with pytest.raises(RuntimeError, match="not configured"):
        await store.write(PointWrite("t1", "setpoint", 1, "collector"))
    assert store.read("t1", "setpoint").version == 0


async def test_reject_nan_and_duplicate_points() -> None:
    definition = PointDefinition("t1", "power", PointRole.FEEDBACK)
    with pytest.raises(ValueError, match="duplicate"):
        PointStore([definition, definition])
    store = PointStore([definition])
    with pytest.raises(ValueError, match="non-finite"):
        await store.update_feedback("t1", "power", float("nan"))
