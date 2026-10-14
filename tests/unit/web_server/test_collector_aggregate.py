"""CollectorStatusAggregator 低频快照聚合测试。"""

from types import SimpleNamespace

from wind_hub_server.application.monitoring.aggregate import CollectorStatusAggregator
from wind_hub_server.application.port.monitoring import (
    AcquisitionStatus,
    CounterSnapshot,
    MetricsSnapshot,
)
from wind_hub_server.application.port.worker import (
    CollectorInfo,
    CollectorRuntimeStatus,
    CollectorTaskSummary,
    DeviceRuntimeInfo,
    SinkRuntimeInfo,
)


class _Collector:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self.config_status_calls = 0

    async def config_status(self) -> CollectorInfo:
        self.config_status_calls += 1
        return CollectorInfo(
            component="collector",
            collector_id=self.worker_id,
            boot_id="boot-1",
            config_hash="hash",
            active_config_hash="hash",
            prepared_config_hash=None,
            boot_config_hash="hash",
            config_revision=None,
            active_revision="",
            prepared_revision=None,
            runtime_running=True,
        )

    async def runtime_status(self) -> CollectorRuntimeStatus:
        return CollectorRuntimeStatus(
            running=True,
            device_count=1,
            sink_count=1,
            devices_connected=1,
            sinks_healthy=1,
            points_collected=10,
            points_routed=9,
            points_dropped=1,
            acquisitions=[
                AcquisitionStatus(
                    instance_id="t1:d1",
                    task_id="t1",
                    device_id="d1",
                    point_group="fast",
                    running=True,
                    consecutive_failures=0,
                    last_error=None,
                    last_duration=None,
                )
            ],
        )

    async def metrics_snapshot(self) -> MetricsSnapshot:
        return MetricsSnapshot(
            counters=CounterSnapshot(
                points_total=10,
                points_bad=1,
                acquisition_runs=2,
            )
        )

    async def list_devices(self) -> list[DeviceRuntimeInfo]:
        return [
            DeviceRuntimeInfo(
                device_id="d1",
                protocol="ads",
                connected=True,
                last_seen=None,
                consecutive_failures=0,
                last_error=None,
            )
        ]

    async def list_sinks(self) -> list[SinkRuntimeInfo]:
        return [
            SinkRuntimeInfo(
                name="archive",
                healthy=True,
                message=None,
                queue_depth=2,
            )
        ]

    async def list_tasks(self) -> list[CollectorTaskSummary]:
        return [
            CollectorTaskSummary(
                task_id="t1",
                device="d1",
                device_group=None,
                point_group="fast",
                interval=1.0,
                targets=["archive"],
                enabled=True,
                runtime_state="running",
                instance_count=1,
                running_instances=1,
                stopped_instances=0,
                failed_instances=0,
            )
        ]


class _Directory:
    def __init__(self, collector: _Collector) -> None:
        self.collector = collector

    def list_worker_ids(self) -> list[str]:
        return [self.collector.worker_id]

    def get(self, worker_id: str) -> _Collector:
        assert worker_id == self.collector.worker_id
        return self.collector


class _Assignments:
    def task_ids_for_worker(self, worker_id: str) -> list[str]:
        return ["t1"]

    def worker_ids_for_device(self, device_id: str) -> list[str]:
        return ["collector-a"]

    def worker_ids_for_sink(self, sink_name: str) -> list[str]:
        return ["collector-a"]


async def test_snapshot_verifies_identity_once_and_aggregates_all_views() -> None:
    collector = _Collector("collector-a")
    config = SimpleNamespace(
        current_config=SimpleNamespace(
            devices={"d1": SimpleNamespace(device_id="d1", protocol="ads")},
            sinks={"archive": SimpleNamespace(name="archive")},
            system=SimpleNamespace(),
        )
    )
    aggregate = CollectorStatusAggregator(
        _Directory(collector),
        _Assignments(),
        config,
    )

    snapshot = await aggregate.snapshot()

    assert collector.config_status_calls == 1
    assert snapshot.runtime_status.points_collected == 10
    assert snapshot.runtime_status.devices_connected == 1
    assert snapshot.runtime_status.sinks_healthy == 1
    assert snapshot.metrics.counters.points_total == 10
    assert snapshot.devices[0].connected is True
    assert snapshot.sinks[0].queue_depth == 2
    assert snapshot.tasks[0].assigned_worker_id == "collector-a"
