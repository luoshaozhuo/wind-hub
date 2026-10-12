"""Collector 测点配置查询组件测试。"""

from __future__ import annotations

from collector.application.config_service import CollectorConfigService
from collector.application.metadata import CollectorMetadataService
from tests.support.new_collector import make_collector_config, make_runtime


def _metadata():
    config = make_collector_config()
    runtime, registry = make_runtime(config)
    state = {"config": config, "hash": "h1"}
    service = CollectorConfigService(
        runtime,
        config,
        load_config=lambda: state["config"],
        config_digest=lambda: state["hash"],
        config_hash="h1",
    )
    return CollectorMetadataService(service), service, runtime, state


async def test_metadata_is_read_only_and_preserves_request_order() -> None:
    metadata, _, runtime, _ = _metadata()
    result = metadata.get_points_metadata("dev1", ["p1", "missing", "p1"])
    assert result.active_revision == "startup"
    assert result.active_config_hash == "h1"
    assert [row.point_id for row in result.points] == ["p1", "missing", "p1"]
    assert result.points[0].metadata is not None
    assert result.points[1].metadata is None
    assert result.points[2].metadata == result.points[0].metadata
    await runtime.stop()


async def test_metadata_uses_committed_version_after_reload() -> None:
    metadata, service, runtime, state = _metadata()
    before = metadata.get_points_metadata("dev1", ["p1"])
    assert before.active_config_hash == "h1"
    state["config"] = make_collector_config(tasks={})
    state["hash"] = "h2"
    assert (await service.prepare_config("r2")).success
    prepared = metadata.get_points_metadata("dev1", ["p1"])
    assert prepared == before
    assert (await service.activate_config("r2")).success
    after = metadata.get_points_metadata("dev1", ["p1"])
    assert after.active_config_hash == "h2"
    assert after.active_revision == "r2"
    assert after.points[0].metadata is not None
    assert after.points[0].metadata.task_ids == ()
    await runtime.stop()
