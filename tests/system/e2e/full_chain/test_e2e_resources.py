"""R4-E：正式 CLI 六服务现场的 /proc 资源观测与完整进程树释放。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.cli.test_r1f3_strict_cli_e2e import SERVICES
from tests.sink_service.system.conftest import role_pids
from tests.support.proc_resources import (
    capture_process_trees,
    sample_process_trees,
    wait_process_trees_reaped,
)

from .conftest import FullChainHarness, _build_and_start, _teardown, decode_kafka_wide_point

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]


@pytest.fixture(scope="module")
def resource_chain(tmp_path_factory: pytest.TempPathFactory) -> Iterator[FullChainHarness]:
    """独立现场允许用例正式 stop_all，而不影响 session 级 full_chain。"""
    harness = _build_and_start(tmp_path_factory.mktemp("resource-chain"))
    try:
        yield harness
    finally:
        _teardown(harness)


async def test_r4e_resources_and_formal_stop_all_release_complete_trees(
    resource_chain: FullChainHarness,
) -> None:
    harness = resource_chain
    main_pids: dict[str, int] = {}
    for service in SERVICES:
        data = await harness.poll(
            harness.cli_rt,
            "service",
            "status",
            service,
            predicate=lambda value, service=service: bool(value)
            and value[0]["process"]["running"] is True
            and (
                value[0]["business"]["health"] == "HEALTHY"
                or service.startswith("collector_")
                and value[0]["business"]["health"] == "DEGRADED"
            ),
            timeout=75,
        )
        process = data[0]["process"]
        pid = process["pid"]
        assert isinstance(pid, int) and pid > 1
        assert process["state"] == "RUNNING"
        main_pids[service] = pid
    assert set(main_pids) == set(SERVICES)
    assert len(set(main_pids.values())) == len(SERVICES)

    sink_workers = role_pids(main_pids["sink"])
    assert set(sink_workers) == {"realtime", "reliable"}
    assert len(set(sink_workers.values())) == 2
    trees = capture_process_trees(main_pids)
    sink_tree = trees["sink"]
    assert set(sink_workers.values()) <= set(sink_tree)
    tracker_pids = sorted(
        pid for pid, identity in sink_tree.items() if "resource_tracker" in identity.command
    )
    assert tracker_pids, "Sink spawn resource_tracker is absent from the captured tree"

    assert harness.sink is not None
    generation_id: str | None = None
    activity_started = datetime.now(UTC)

    def is_fresh_kafka(record: Any) -> bool:
        item = decode_kafka_wide_point(record, device_id="WTG_SIM01", point_id="wind_speed")
        return item is not None and (
            datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= activity_started
        )

    try:
        # 采样期间持续产生真实 RT 与 Reliable 流量，而非空闲进程快照。
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "start", "WTG_SIM01", "--yes")
        started = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(started["data"]["generation_id"])
        harness.generation_id = generation_id
        redis_sample = await harness.sink.wait_redis_value(
            harness.redis_key("WTG_SIM01", "wind_speed"),
            timeout=90,
            match=lambda item: datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00"))
            >= activity_started,
        )
        kafka_records = await harness.sink.wait_kafka_messages(
            min_messages=1, timeout=90, match=is_fresh_kafka
        )
        kafka_sample = next(
            item
            for record in kafka_records
            if is_fresh_kafka(record)
            and (
                item := decode_kafka_wide_point(
                    record, device_id="WTG_SIM01", point_id="wind_speed"
                )
            )
            is not None
        )
        assert redis_sample["collector_id"] == harness.rt_collector
        assert kafka_sample["data_level"] == "protocol"
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.poll(
                config,
                "service",
                "status",
                "collector_realtime" if config == harness.cli_rt else "collector_reliable",
                predicate=lambda value: bool(value) and value[0]["business"]["health"] == "HEALTHY",
                timeout=60,
            )

        observed = await sample_process_trees(trees, duration_seconds=12.0)
    finally:
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt,
                "data-generator",
                "stop",
                generation_id,
                "--yes",
                check=False,
            )
            harness.generation_id = None
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "stop", "WTG_SIM01", "--yes", check=False)

    assert 10.0 <= observed["duration_seconds"] <= 15.0
    assert observed["sample_count"] >= 2
    assert len(observed["timeline"]) == observed["sample_count"]
    assert {name: value["process_count"] for name, value in observed["services"].items()} == {
        name: len(tree) for name, tree in trees.items()
    }
    for frame in observed["timeline"]:
        assert frame["monotonic_offset_seconds"] >= 0
        for service in SERVICES:
            current = frame["services"][service]
            assert set(current) == {
                "cumulative_cpu_seconds",
                "delta_cpu_percent",
                "rss_bytes",
                "fd_count",
                "process_count",
            }
    harness.record(
        "R4-E-RESOURCES",
        main_pids=main_pids,
        sink_worker_pids=sink_workers,
        resource_tracker_pids=tracker_pids,
        generation_id=generation_id,
        activity={
            "realtime_task": "WTG_SIM01",
            "reliable_task": "WTG_SIM01",
            "redis_sample": redis_sample,
            "kafka_sample": kafka_sample,
        },
        observations=observed,
        assertions={
            "six_main_pids_from_formal_cli": True,
            "sink_has_realtime_and_reliable_workers": True,
            "resource_tracker_included": True,
            "fresh_redis_and_kafka_samples_before_sampling": True,
            "per_frame_resource_timeline_recorded": True,
            "tasks_and_generation_stopped_after_sampling": True,
            "performance_thresholds": "none; observations only",
        },
    )

    # stop_all 使用生产 CLI；随后逐服务读取正式状态，不以进程句柄推断停止。
    await harness.cli.stop_all(harness.cli_rt)
    for service in SERVICES:
        stopped = await harness.cli_call(harness.cli_rt, "service", "status", service, check=False)
        payload: dict[str, Any] = stopped["data"][0]
        assert payload["process"]["running"] is False
        assert payload["process"]["state"] == "STOPPED"
    await wait_process_trees_reaped(trees)
    harness.record(
        "R4-E-RESOURCE-CLEANUP",
        main_pids=main_pids,
        sink_worker_pids=sink_workers,
        resource_tracker_pids=tracker_pids,
        released_pids=sorted({pid for tree in trees.values() for pid in tree}),
        assertions={
            "formal_stop_all_completed": True,
            "all_cli_process_states_stopped": True,
            "all_original_identities_reaped_including_resource_tracker": True,
        },
    )
