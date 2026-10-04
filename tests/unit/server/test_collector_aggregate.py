"""CollectorStatusAggregator 低频快照聚合测试。"""

from types import SimpleNamespace

from wind_hub_server.application.monitoring.aggregate import CollectorStatusAggregator


class _Collector:
    def __init__(self, worker_id: str) -> None:
        self.worker_id = worker_id
        self.config_status_calls = 0

    async def config_status(self):
        self.config_status_calls += 1
        return {"collector_id": self.worker_id}

    async def runtime_status(self):
        return {
            "running": True,
            "points_collected": 10,
            "points_routed": 9,
            "points_dropped": 1,
            "acquisitions": [
                {
                    "instance_id": "t1:d1",
                    "task_id": "t1",
                    "device_id": "d1",
                    "point_group": "fast",
                }
            ],
        }

    async def metrics_snapshot(self):
        return {
            "counters": {
                "points_total": 10,
                "points_bad": 1,
                "acquisition_runs": 2,
                "acquisition_failures": 0,
                "acquisition_partial": 0,
                "missed_cycles": 0,
                "poll_overruns": 0,
                "connect_failures": 0,
                "reconnects": 0,
            },
            "device_connect_failures": {},
            "device_reconnects": {},
            "events": [],
        }

    async def list_devices(self):
        return [{"device_id": "d1", "connected": True}]

    async def list_sinks(self):
        return [{"name": "archive", "healthy": True, "queue_depth": 2}]

    async def list_tasks(self):
        return [
            {
                "task_id": "t1",
                "device": "d1",
                "device_group": None,
                "point_group": "fast",
                "interval": 1.0,
                "targets": ["archive"],
                "enabled": True,
                "runtime_state": "running",
                "instance_count": 1,
                "running_instances": 1,
                "stopped_instances": 0,
                "failed_instances": 0,
            }
        ]


class _Directory:
    def __init__(self, collector: _Collector) -> None:
        self.collector = collector

    def list_worker_ids(self):
        return [self.collector.worker_id]

    def get(self, worker_id: str):
        assert worker_id == self.collector.worker_id
        return self.collector


class _Assignments:
    def task_ids_for_worker(self, worker_id: str):
        return ["t1"]

    def worker_ids_for_device(self, device_id: str):
        return ["collector-a"]

    def worker_ids_for_sink(self, sink_name: str):
        return ["collector-a"]


async def test_snapshot_verifies_identity_once_and_aggregates_all_views() -> None:
    collector = _Collector("collector-a")
    config = SimpleNamespace(
        current_config=SimpleNamespace(
            devices=SimpleNamespace(
                devices=[
                    SimpleNamespace(
                        device_id="d1",
                        protocol="ads",
                    )
                ]
            ),
            sinks=SimpleNamespace(
                sinks=[SimpleNamespace(name="archive")]
            ),
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
    assert snapshot["runtime_status"]["points_collected"] == 10
    assert snapshot["runtime_status"]["devices_connected"] == 1
    assert snapshot["runtime_status"]["sinks_healthy"] == 1
    assert snapshot["metrics"]["counters"]["points_total"] == 10
    assert snapshot["devices"][0]["connected"] is True
    assert snapshot["sinks"][0]["queue_depth"] == 2
    assert snapshot["tasks"][0]["assigned_worker_id"] == "collector-a"
