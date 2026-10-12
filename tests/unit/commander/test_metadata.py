"""Commander 配置元数据纯查询与 generation 一致性测试。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from commander.application.errors import CommandError
from commander.application.metadata import CommanderMetadataService, _point_metadata
from commander.application.runtime import CommanderRuntime
from core.domain import PointAccess
from tests.support.new_commander import FakeRegistry, make_commander_config


def _service() -> tuple[CommanderMetadataService, CommanderRuntime, FakeRegistry]:
    registry = FakeRegistry()
    runtime = CommanderRuntime(
        make_commander_config(),
        config_hash="hash-a",
        protocol_registry=registry,
    )
    return CommanderMetadataService(runtime), runtime, registry


async def test_metadata_is_config_only_and_preserves_order() -> None:
    service, runtime, registry = _service()
    result = await service.get_points_metadata("dev1", ["p1", "missing", "p1"])
    assert result.active_revision == "startup"
    assert result.active_config_hash == "hash-a"
    assert [row.point_id for row in result.points] == ["p1", "missing", "p1"]
    assert result.points[0].metadata is not None
    assert result.points[0].metadata.access in {access.value for access in PointAccess}
    assert result.points[1].metadata is None
    assert result.points[2].metadata == result.points[0].metadata
    assert registry.instances[0].connect_calls == 0
    assert registry.instances[0].writes == []
    assert result.points[0].metadata.data_type == "float32"
    assert not runtime.devices["dev1"].is_open()


async def test_metadata_unknown_device_and_invalid_request() -> None:
    service, _, _ = _service()
    with pytest.raises(CommandError, match="unknown device"):
        await service.get_points_metadata("absent", ["p1"])
    with pytest.raises(ValueError, match="point_ids"):
        await service.get_points_metadata("dev1", [])
    with pytest.raises(ValueError, match="point_ids"):
        await service.get_points_metadata("dev1", [""])


async def test_metadata_pinned_generation_during_activation() -> None:
    service, runtime, _ = _service()
    before = await service.get_points_metadata("dev1", ["p1"])
    async with runtime.operation():
        await runtime.prepare_config("r2", make_commander_config(), "hash-b")
        await runtime.activate_config("r2")
        pinned = await service.get_points_metadata("dev1", ["p1"])
        assert pinned.active_revision == before.active_revision
        assert pinned.active_config_hash == before.active_config_hash
        assert pinned.points == before.points

    after = await service.get_points_metadata("dev1", ["p1"])
    assert after.active_revision == "r2"
    assert after.active_config_hash == "hash-b"
    await runtime.stop()


async def test_metadata_reports_configured_access_not_live_capability() -> None:
    service, runtime, registry = _service()
    point = runtime.devices["dev1"].point_table.points["p1"]
    for access in PointAccess:
        changed_point = replace(point, access=access)
        assert _point_metadata(changed_point, data_type="float32").access == access.value
    assert registry.instances[0].connect_calls == 0
