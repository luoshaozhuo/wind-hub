"""新 Collector 应用服务（query/task/sink）与指标状态单元测试。"""

from __future__ import annotations

import pytest

from collector.application.identity import build_collector_identity, default_collector_id
from collector.application.metrics_state import CollectorMetricsState
from collector.application.services import (
    CollectorQueryService,
    CollectorSinkService,
    CollectorTaskService,
)
from collector.application.task_instance import TaskInstanceState
from tests.support.new_collector import (
    FakeSink,
    make_collector_config,
    make_runtime,
)

# ---------------------------------------------------------------------------
# identity / metrics
# ---------------------------------------------------------------------------


def test_identity_defaults_to_hostname_when_no_env(monkeypatch):
    monkeypatch.delenv("WIND_HUB_COLLECTOR_ID", raising=False)
    assert default_collector_id()


def test_identity_env_override(monkeypatch):
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ID", "collector-7")
    assert default_collector_id() == "collector-7"


def test_build_identity_uses_given_hash_and_id():
    identity = build_collector_identity("abc123", collector_id="c1")
    assert identity.collector_id == "c1"
    assert identity.config_hash == "abc123"
    assert identity.boot_id
    assert identity.config_revision is None


def test_metrics_state_counters_and_events():
    metrics = CollectorMetricsState(event_capacity=3)
    metrics.observe_collected(5)
    metrics.observe_bad(2)
    metrics.acquisition_run_finished("dev1", "g", "failed", 0.1)
    metrics.acquisition_run_finished("dev1", "g", "partial", 0.1)
    metrics.acquisition_run_finished("dev1", "g", "success", 0.1)
    metrics.acquisition_poll_stats("dev1", "g", 0.01, overrun=True, missed=2)
    metrics.device_connect_failed("dev1", "modbus")
    metrics.device_reconnected("dev1", "modbus")

    snapshot = metrics.snapshot()
    counters = snapshot["counters"]
    assert counters["points_total"] == 5
    assert counters["points_bad"] == 2
    assert counters["acquisition_runs"] == 3
    assert counters["acquisition_failures"] == 1
    assert counters["acquisition_partial"] == 1
    assert counters["missed_cycles"] == 2
    assert counters["poll_overruns"] == 1
    assert counters["connect_failures"] == 1
    assert counters["reconnects"] == 1
    assert snapshot["device_connect_failures"] == {"dev1": 1}
    assert snapshot["device_reconnects"] == {"dev1": 1}
    # 事件容量 3：只保留最近 3 条
    assert len(snapshot["events"]) == 3
    kinds = [event["kind"] for event in snapshot["events"]]
    assert "reconnected" in kinds


# ---------------------------------------------------------------------------
# task service
# ---------------------------------------------------------------------------


async def test_task_service_summary_and_lifecycle():
    config = make_collector_config()
    runtime, _ = make_runtime(config)
    service = CollectorTaskService(runtime)

    summaries = await service.list_task_summaries()
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.task_id == "t1"
    assert summary.device == "dev1"
    assert summary.interval == 1.0
    assert summary.targets == ["s1"]
    assert summary.enabled is True

    await runtime.start()

    summary = await service.get_task_summary("t1")
    assert summary.instance_count == 1
    assert summary.runtime_state == "stopped"
    assert summary.running_instances == 0
    assert summary.stopped_instances == 1

    summary = await service.start_task("t1")
    assert summary.runtime_state == "running"
    assert summary.running_instances == 1

    instances = await service.list_task_instances("t1")
    assert len(instances) == 1
    assert instances[0].state is TaskInstanceState.RUNNING

    detail = await service.stop_instance(instances[0].instance_id)
    assert detail.state is TaskInstanceState.STOPPED

    summary = await service.get_task_summary("t1")
    assert summary.runtime_state == "stopped"

    await runtime.stop()


async def test_task_service_unknown_ids_raise_key_error():
    config = make_collector_config()
    runtime, _ = make_runtime(config)
    service = CollectorTaskService(runtime)

    with pytest.raises(KeyError):
        await service.get_task_summary("ghost")
    with pytest.raises(KeyError):
        await service.get_instance("ghost")
    with pytest.raises(KeyError):
        await service.start_task("ghost")
    with pytest.raises(KeyError):
        await service.stop_instance("ghost")


async def test_task_service_disabled_task_cannot_start():
    from collector.application.config import CollectionTask

    config = make_collector_config(
        tasks={
            "t1": CollectionTask(
                task_id="t1",
                device="dev1",
                point_group="g",
                interval=1.0,
                targets=("s1",),
                enabled=False,
            )
        }
    )
    runtime, _ = make_runtime(config)
    service = CollectorTaskService(runtime)
    with pytest.raises(ValueError, match="disabled"):
        await service.start_task("t1")


# ---------------------------------------------------------------------------
# query / sink service
# ---------------------------------------------------------------------------


async def test_query_service_status_and_devices():
    config = make_collector_config()
    sink = FakeSink()
    runtime, _ = make_runtime(config, sinks={"s1": sink})
    query = CollectorQueryService(runtime)

    await runtime.start()
    status = await query.status()
    assert status.running is True
    assert status.device_count == 1
    assert status.sink_count == 1
    assert status.devices_connected == 1
    assert status.sinks_healthy == 1

    devices = await query.list_devices()
    assert len(devices) == 1
    assert devices[0].device_id == "dev1"
    assert devices[0].protocol == "modbus"
    assert devices[0].connected is True

    await runtime.stop()


async def test_sink_service_list_verify_and_write_test():
    config = make_collector_config()
    sink = FakeSink()
    runtime, _ = make_runtime(config, sinks={"s1": sink})
    service = CollectorSinkService(runtime)

    await runtime.start()

    items = await service.list_sinks()
    assert len(items) == 1
    assert items[0].name == "s1"
    assert items[0].healthy is True

    info = await service.verify_sink("s1")
    assert info.healthy is True

    result = await service.write_test_sink("s1")
    assert result.success is True
    assert sink.batches, "诊断写应到达 Sink"
    written = sink.batches[-1][0]
    assert written.device_id == "_diagnostic"
    assert written.point_id == "_write_test"
    assert sink.flush_calls >= 1

    with pytest.raises(KeyError):
        await service.verify_sink("ghost")
    with pytest.raises(KeyError):
        await service.write_test_sink("ghost")

    await runtime.stop()
