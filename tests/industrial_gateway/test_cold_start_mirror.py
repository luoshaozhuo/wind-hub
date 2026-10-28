"""冷启动镜像只用于首次读；拒绝命令和失效反馈不能回退到零。"""

from __future__ import annotations

import pytest

from industrial_gateway.application.point_store import PointStore
from industrial_gateway.domain import PointDefinition, PointRole, PointWrite

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_failed_command_never_looks_like_unissued_command() -> None:
    async def reject(_: PointWrite) -> None:
        raise ValueError("command rejected")

    store = PointStore(
        [PointDefinition("wt", "ctrl", PointRole.COMMAND, writable=True)],
        command_handler=reject,
    )
    assert store.is_uninitialized_command("wt", "ctrl")
    with pytest.raises(ValueError, match="rejected"):
        await store.write(PointWrite("wt", "ctrl", 1, "test"))
    assert not store.is_uninitialized_command("wt", "ctrl")
    assert store.read("wt", "ctrl").value is None


@pytest.mark.asyncio
async def test_expired_or_invalidated_feedback_cannot_use_cold_start_mirror() -> None:
    store = PointStore([PointDefinition("wt", "power", PointRole.FEEDBACK)])
    assert store.is_uninitialized_feedback("wt", "power")
    await store.update_feedback("wt", "power", 3.0)
    assert not store.is_uninitialized_feedback("wt", "power")
    await store.invalidate_feedback("wt", "power")
    assert store.read("wt", "power").value is None
    assert not store.is_uninitialized_feedback("wt", "power")


@pytest.mark.asyncio
async def test_rejected_command_batch_disables_cold_mirror() -> None:
    from industrial_gateway.application.write_service import (
        ResolvedWrite,
        WriterRole,
        WriteService,
    )

    store = PointStore(
        [
            PointDefinition("wt", "start", PointRole.COMMAND, writable=True),
            PointDefinition("wt", "stop", PointRole.COMMAND, writable=True),
        ]
    )
    service = WriteService(
        store,
        {
            ("wt", "start"): PointRole.COMMAND,
            ("wt", "stop"): PointRole.COMMAND,
        },
    )
    with pytest.raises(PermissionError, match="exactly one"):
        await service.write(
            ResolvedWrite(
                {("wt", "start"): True, ("wt", "stop"): True},
                WriterRole.COMMAND,
            )
        )
    assert not store.is_uninitialized_command("wt", "start")
    assert not store.is_uninitialized_command("wt", "stop")
    assert store.read("wt", "start").value is None
