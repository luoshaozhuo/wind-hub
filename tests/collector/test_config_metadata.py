"""Collector 测点配置查询组件测试。"""

from __future__ import annotations

import pytest

from collector.application.config_service import CollectorConfigService
from collector.application.metadata import CollectorMetadataService
from tests.support.new_collector import make_collector_config, make_runtime

pytestmark = pytest.mark.component


def _metadata():
    config = make_collector_config()
    runtime, _ = make_runtime(config)
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
    protocol = runtime.devices["dev1"].protocol
    assert protocol.connect_calls == 0  # type: ignore[attr-defined]
    assert protocol.writes == []  # type: ignore[attr-defined]
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


async def test_metadata_unchanged_after_failed_activate() -> None:
    """reconfigure 失败时基线不提交，元数据查询仍返回旧配置。"""
    metadata, service, runtime, state = _metadata()
    await runtime.start()
    before = metadata.get_points_metadata("dev1", ["p1"])

    # 新配置移除 dev1、新增 dev2；session_factory 缺失使 device 阶段失败。
    runtime._session_factory = None  # type: ignore[attr-defined]
    state["config"] = make_collector_config(device_id="dev2", tasks={})
    state["hash"] = "h2"
    assert (await service.prepare_config("r-fail")).success
    assert not (await service.activate_config("r-fail")).success

    after = metadata.get_points_metadata("dev1", ["p1"])
    assert after == before
    assert after.active_config_hash == "h1"
    await runtime.stop()


async def test_metadata_unchanged_after_abort() -> None:
    metadata, service, runtime, state = _metadata()
    before = metadata.get_points_metadata("dev1", ["p1"])
    state["config"] = make_collector_config(tasks={})
    state["hash"] = "h2"
    assert before.points[0].metadata is not None
    assert (await service.prepare_config("r-abort")).success
    assert await service.abort_config("r-abort") is True
    assert metadata.get_points_metadata("dev1", ["p1"]) == before
    await runtime.stop()


async def test_metadata_query_during_activate_never_mixes_versions() -> None:
    """activate 提交段无 await；并发查询只能看到完整旧版或完整新版。"""
    import asyncio

    metadata, service, runtime, state = _metadata()
    state["config"] = make_collector_config(tasks={})
    state["hash"] = "h2"
    assert (await service.prepare_config("r-conc")).success

    observations: list[tuple[str, str, tuple[str, ...]]] = []

    async def probe() -> None:
        for _ in range(50):
            row = metadata.get_points_metadata("dev1", ["p1"])
            task_ids = row.points[0].metadata.task_ids if row.points[0].metadata else ()
            observations.append((row.active_revision, row.active_config_hash, task_ids))
            await asyncio.sleep(0)

    async def activate() -> None:
        for _ in range(20):
            await asyncio.sleep(0)
        assert (await service.activate_config("r-conc")).success

    await asyncio.gather(probe(), activate())
    assert observations, "probe must record at least one observation"
    for revision, config_hash, task_ids in observations:
        # 版本/hash/测点关联必须来自同一次提交，不允许交叉组合。
        assert (revision, config_hash) in {("startup", "h1"), ("r-conc", "h2")}
        assert task_ids == (() if revision == "r-conc" else ("t1",))
    assert observations[-1][0] == "r-conc"
    await runtime.stop()


async def test_metadata_never_triggers_field_io() -> None:
    """所有现场 I/O 入口替换为调用即失败，元数据查询仍必须成功。"""
    from typing import Any

    from tests.support.new_collector import CollectorFakeProtocol

    class FailOnCallProtocol(CollectorFakeProtocol):
        def __getattribute__(self, name: str) -> Any:
            if name in {"connect", "disconnect", "read", "write", "subscribe", "interrogate"}:
                raise AssertionError(f"field I/O must not be called: {name}")
            return super().__getattribute__(name)

    config = make_collector_config()
    runtime, _ = make_runtime(config, protocol=FailOnCallProtocol())
    service = CollectorConfigService(
        runtime,
        config,
        load_config=lambda: config,
        config_digest=lambda: "h1",
        config_hash="h1",
    )
    metadata = CollectorMetadataService(service)
    result = metadata.get_points_metadata("dev1", ["p1"])
    assert result.points[0].metadata is not None
    await runtime.stop()


async def test_metadata_reflects_task_changes_after_reload() -> None:
    """Task 停用在 activate 后正确反映到采集侧元数据。

    Sink 绑定已移出 Collector（归属 sink_service），Collector 元数据只报告
    覆盖该点的启用 Task 列表。
    """
    from collector.application.config import CollectionTask
    from core.domain import DeviceGroupId

    config = make_collector_config()
    runtime, _ = make_runtime(config)
    state: dict[str, object] = {"config": config, "hash": "h1"}
    service = CollectorConfigService(
        runtime,
        config,
        load_config=lambda: state["config"],  # type: ignore[return-value]
        config_digest=lambda: state["hash"],  # type: ignore[return-value]
        config_hash="h1",
    )
    metadata = CollectorMetadataService(service)
    before = metadata.get_points_metadata("dev1", ["p1"])
    assert before.points[0].metadata is not None
    assert before.points[0].metadata.task_ids == ("t1",)

    disabled_task = CollectionTask(
        task_id="t1",
        device_group_id=DeviceGroupId("dev1"),
        point_group="g",
        interval=1.0,
        enabled=False,
    )
    state["config"] = make_collector_config(tasks={"t1": disabled_task})
    state["hash"] = "h2"
    assert (await service.prepare_config("r3")).success
    assert (await service.activate_config("r3")).success
    after = metadata.get_points_metadata("dev1", ["p1"])
    assert after.points[0].metadata is not None
    assert after.points[0].metadata.task_ids == ()
    await runtime.stop()
