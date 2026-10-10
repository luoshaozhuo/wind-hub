"""新 Collector CollectorConfigService 热重载编排组件测试。"""

from __future__ import annotations

from collector.application.config import CollectionTask
from collector.application.config_service import CollectorConfigService
from tests.support.new_collector import make_collector_config, make_runtime


def _service(config=None, runtime=None):
    config = config or make_collector_config()
    if runtime is None:
        runtime, _ = make_runtime(config)
    state = {"config": config, "hash": "h1"}
    service = CollectorConfigService(
        runtime,
        config,
        load_config=lambda: state["config"],
        config_digest=lambda: state["hash"],
        config_hash=state["hash"],
    )
    return service, runtime, state


async def test_prepare_activate_commits_new_baseline():
    service, runtime, state = _service()
    new_config = make_collector_config(tasks={})
    state["config"] = new_config
    state["hash"] = "h2"

    prepared = await service.prepare_config("r1")
    assert prepared.success and prepared.diff.tasks.removed == ["t1"]
    assert service.prepared_revision == "r1"

    activated = await service.activate_config("r1")
    assert activated.success
    assert service.current_config is new_config
    assert service.config_hash == "h2"
    assert service.active_revision == "r1"
    assert service.prepared_revision is None
    await runtime.stop()


async def test_reload_one_shot_prepare_and_activate():
    service, runtime, state = _service()
    new_config = make_collector_config(
        tasks={
            "t1": CollectionTask(
                task_id="t1", device="dev1", point_group="g", interval=2.0, targets=("s1",)
            )
        }
    )
    state["config"] = new_config
    state["hash"] = "h2"
    result = await service.reload()
    assert result.success
    assert service.current_config is new_config
    assert service.active_revision == "local-1"
    await runtime.stop()


async def test_prepare_toctou_detects_change_during_load():
    config = make_collector_config()
    runtime, _ = make_runtime(config)
    hashes = iter(["h1", "h2"])  # load 前后指纹不同
    service = CollectorConfigService(
        runtime,
        config,
        load_config=lambda: config,
        config_digest=lambda: next(hashes),
        config_hash="h1",
    )
    result = await service.prepare_config("r1")
    assert not result.success
    assert "config changed while preparing" in result.errors[0]
    await runtime.stop()


async def test_prepare_expected_hash_mismatch_rejected():
    service, runtime, _ = _service()
    result = await service.prepare_config("r1", expected_config_hash="other")
    assert not result.success
    assert "config hash mismatch" in result.errors[0]
    await runtime.stop()


async def test_prepare_load_failure_aborts_without_side_effects():
    config = make_collector_config()
    runtime, _ = make_runtime(config)

    def broken():
        raise ValueError("bad yaml")

    service = CollectorConfigService(
        runtime, config, load_config=broken, config_digest=lambda: "h1", config_hash="h1"
    )
    result = await service.prepare_config("r1")
    assert not result.success and "bad yaml" in result.errors[0]
    assert service.current_config is config
    await runtime.stop()


async def test_activate_requires_matching_prepared_revision():
    service, runtime, _ = _service()
    result = await service.activate_config("rX")
    assert not result.success
    assert "prepared revision mismatch" in result.errors[0]

    await service.prepare_config("r1")
    result = await service.activate_config("r2")
    assert not result.success
    await runtime.stop()


async def test_abort_clears_prepared_idempotently():
    service, runtime, _ = _service()
    await service.prepare_config("r1")
    assert await service.abort_config("r1") is True
    assert service.prepared_revision is None
    assert await service.abort_config("r1") is False
    await runtime.stop()


async def test_partial_reconfigure_failure_keeps_baseline():
    config = make_collector_config()
    runtime, _ = make_runtime(config)
    await runtime.start()
    state = {"config": config, "hash": "h1"}
    service = CollectorConfigService(
        runtime,
        config,
        load_config=lambda: state["config"],
        config_digest=lambda: state["hash"],
        config_hash="h1",
    )

    # 新配置：移除 dev1、新增 dev2，但 session_factory 缺失 → device 阶段报错
    runtime._session_factory = None
    new_config = make_collector_config(device_id="dev2", tasks={})
    state["config"] = new_config
    state["hash"] = "h2"

    result = await service.reload()
    assert not result.success
    assert any("device:" in e for e in result.errors)
    assert service.current_config is config  # 基线不提交
    assert service.config_hash == "h1"
    await runtime.stop()
