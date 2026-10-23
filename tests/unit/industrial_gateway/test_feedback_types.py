"""Gateway 遥测类型边界与冷启动命令读镜像。"""
from __future__ import annotations

import pytest

from industrial_gateway.application.point_store import PointStore
from industrial_gateway.domain import PointDefinition, PointRole


@pytest.mark.asyncio
async def test_invalid_float_for_int32_is_atomic() -> None:
    store = PointStore([
        PointDefinition("wt1", "status", PointRole.FEEDBACK, data_type="int32"),
        PointDefinition("wt1", "power", PointRole.FEEDBACK, data_type="float32"),
    ])
    await store.update_batch({("wt1", "status"): 2}, source="test")
    with pytest.raises(ValueError, match="int32"):
        await store.update_batch(
            {("wt1", "power"): 9.0, ("wt1", "status"): 2.0},
            source="test",
        )
    assert store.read("wt1", "status").value == 2
    assert store.read("wt1", "power").value is None


def test_uninitialized_command_has_no_stored_value() -> None:
    store = PointStore([
        PointDefinition("wt1", "setpoint", PointRole.COMMAND, writable=True),
    ])
    assert store.is_uninitialized_command("wt1", "setpoint")
    assert store.read("wt1", "setpoint").value is None
