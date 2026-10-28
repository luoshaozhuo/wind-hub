"""基于 TaskPlacement 的多 Collector 低频聚合快照。"""

from __future__ import annotations

import asyncio

from web_server.application.port.sink import SinkDirectoryPort

from web_server.application.config.service import ConfigService
from web_server.application.port.collector_directory import CollectorDirectory
from web_server.application.port.monitoring import (
    AcquisitionStatus,
    CollectorSnapshot,
    CounterSnapshot,
    DeviceRuntimeSnapshot,
    MetricsSnapshot,
    MonitoringEvent,
    RuntimeStatusSnapshot,
    SinkRuntimeSnapshot,
    TaskRuntimeSnapshot,
)
from web_server.application.port.worker import (
    CollectorRuntimeStatus,
    CollectorTaskSummary,
    DeviceRuntimeInfo,
)
from web_server.application.task.placement import TaskPlacementRegistry


class _WorkerFacts:
    """单个 Collector 一次身份校验后的四类只读状态。"""

    def __init__(
        self,
        runtime: CollectorRuntimeStatus,
        metrics: MetricsSnapshot,
        devices: list[DeviceRuntimeInfo],
        tasks: list[CollectorTaskSummary],
    ) -> None:
        self.runtime = runtime
        self.metrics = metrics
        self.devices = devices
        self.tasks = tasks


class CollectorStatusAggregator:
    """每个 Collector 只校验一次身份，再并发读取低频运行快照。"""

    def __init__(
        self,
        collectors: CollectorDirectory,
        placements: TaskPlacementRegistry,
        config: ConfigService,
        sink_directory: SinkDirectoryPort,
    ) -> None:
        self._collectors = collectors
        self._placements = placements
        self._config = config
        self._sink_directory = sink_directory

    async def snapshot(self) -> CollectorSnapshot:
        """返回 MonitoringService 所需的完整 Collector 聚合快照。"""
        worker_ids = self._collectors.list_worker_ids()
        results = await asyncio.gather(
            *(self._worker_facts(worker_id) for worker_id in worker_ids),
            return_exceptions=True,
        )

        by_worker: dict[str, _WorkerFacts] = {}
        unavailable_workers: list[str] = []
        for worker_id, result in zip(worker_ids, results, strict=True):
            if isinstance(result, BaseException):
                unavailable_workers.append(worker_id)
                continue
            by_worker[worker_id] = result

        devices = self._aggregate_devices(by_worker)
        sinks = await self._aggregate_sinks()
        tasks = self._aggregate_tasks(by_worker)
        runtime = self._aggregate_runtime(by_worker, unavailable_workers, devices, sinks)
        metrics = self._aggregate_metrics(by_worker)

        return CollectorSnapshot(
            runtime_status=runtime,
            metrics=metrics,
            devices=devices,
            sinks=sinks,
            tasks=tasks,
        )

    async def _worker_facts(self, worker_id: str) -> _WorkerFacts:
        """校验一次 Collector 身份，然后并发读取本 Worker 的四类只读状态。"""
        collector = self._collectors.get(worker_id)
        status = await collector.config_status()
        reported_id = status.collector_id
        if reported_id != worker_id:
            raise RuntimeError(
                f"collector identity mismatch: expected={worker_id} "
                f"reported={reported_id or '<empty>'}"
            )
        runtime, metrics, devices, tasks = await asyncio.gather(
            collector.runtime_status(),
            collector.metrics_snapshot(),
            collector.list_devices(),
            collector.list_tasks(),
        )
        return _WorkerFacts(runtime, metrics, devices, tasks)

    def _aggregate_runtime(
        self,
        by_worker: dict[str, _WorkerFacts],
        unavailable_workers: list[str],
        devices: list[DeviceRuntimeSnapshot],
        sinks: list[SinkRuntimeSnapshot],
    ) -> RuntimeStatusSnapshot:
        acquisitions: list[AcquisitionStatus] = []
        points_collected = points_routed = points_dropped = 0
        running = not unavailable_workers

        for worker_id, facts in by_worker.items():
            assigned = set(self._placements.task_ids_for_worker(worker_id))
            running = running and facts.runtime.running
            points_collected += facts.runtime.points_collected
            points_routed += facts.runtime.points_routed
            points_dropped += facts.runtime.points_dropped
            acquisitions.extend(
                item for item in facts.runtime.acquisitions if item.task_id in assigned
            )

        return RuntimeStatusSnapshot(
            running=running,
            device_count=len(self._config.current_config.site.devices),
            sink_count=len(self._config.current_config.sinks),
            devices_connected=sum(1 for row in devices if row.connected),
            sinks_healthy=sum(1 for row in sinks if row.healthy),
            points_collected=points_collected,
            points_routed=points_routed,
            points_dropped=points_dropped,
            acquisitions=acquisitions,
            degraded=bool(unavailable_workers),
            unavailable_workers=list(unavailable_workers),
        )

    def _aggregate_devices(
        self,
        by_worker: dict[str, _WorkerFacts],
    ) -> list[DeviceRuntimeSnapshot]:
        worker_devices = {
            worker_id: {row.device_id: row for row in facts.devices}
            for worker_id, facts in by_worker.items()
        }

        snapshot = self._config.current_config
        rows: list[DeviceRuntimeSnapshot] = []
        for cfg in snapshot.site.devices.values():
            model = snapshot.device_models[cfg.device_model_id]
            protocol = snapshot.point_tables[model.point_table_id].protocol.name
            owners = self._placements.worker_ids_for_device(cfg.device_id)
            states = [worker_devices.get(worker_id, {}).get(cfg.device_id) for worker_id in owners]
            present = [row for row in states if row is not None]
            rows.append(
                DeviceRuntimeSnapshot(
                    device_id=cfg.device_id,
                    protocol=protocol,
                    connected=(
                        bool(owners)
                        and len(present) == len(owners)
                        and all(row.connected for row in present)
                    ),
                    consecutive_failures=max(
                        (row.consecutive_failures for row in present),
                        default=0,
                    ),
                    last_error=(
                        next(
                            (row.last_error for row in present if row.last_error),
                            None,
                        )
                        if len(present) == len(owners)
                        else "collector unavailable"
                    ),
                )
            )
        return rows

    async def _aggregate_sinks(self) -> list[SinkRuntimeSnapshot]:
        """Get Sink status from the independent Sink Service, not Collectors."""
        try:
            items = await self._sink_directory.list_sinks()
            statuses = {item["name"]: item for item in items}
        except Exception:
            statuses = {}

        return [
            SinkRuntimeSnapshot(
                name=cfg.sink_id,
                healthy=bool(
                    cfg.enabled
                    and statuses.get(cfg.sink_id, {}).get("state") == "running"
                    and statuses.get(cfg.sink_id, {}).get("nats_connected") is True
                    and not statuses.get(cfg.sink_id, {}).get("failure")
                ),
                message=(
                    str(statuses[cfg.sink_id].get("failure") or statuses[cfg.sink_id]["state"])
                    if cfg.sink_id in statuses
                    else "Sink Service unavailable"
                ),
                queue_depth=0,
            )
            for cfg in self._config.current_config.sinks.values()
        ]

    def _aggregate_tasks(
        self,
        by_worker: dict[str, _WorkerFacts],
    ) -> list[TaskRuntimeSnapshot]:
        """按 placement 过滤各 Collector 的 Task 聚合状态。"""
        rows: list[TaskRuntimeSnapshot] = []
        for worker_id, facts in by_worker.items():
            assigned = set(self._placements.task_ids_for_worker(worker_id))
            rows.extend(
                TaskRuntimeSnapshot(
                    task_id=row.task_id,
                    device=row.device,
                    device_group=row.device_group,
                    point_group=row.point_group,
                    interval=row.interval,
                    targets=row.targets,
                    enabled=row.enabled,
                    runtime_state=row.runtime_state,
                    instance_count=row.instance_count,
                    running_instances=row.running_instances,
                    stopped_instances=row.stopped_instances,
                    failed_instances=row.failed_instances,
                    assigned_worker_id=worker_id,
                )
                for row in facts.tasks
                if row.task_id in assigned
            )
        return rows

    def _aggregate_metrics(
        self,
        by_worker: dict[str, _WorkerFacts],
    ) -> MetricsSnapshot:
        failures: dict[str, int] = {}
        reconnects: dict[str, int] = {}
        events: list[MonitoringEvent] = []

        sums = dict.fromkeys(CounterSnapshot.__dataclass_fields__, 0)
        for worker_id, facts in by_worker.items():
            for field in sums:
                sums[field] += getattr(facts.metrics.counters, field)

            for device_id, value in facts.metrics.device_connect_failures.items():
                if worker_id in self._placements.worker_ids_for_device(device_id):
                    failures[device_id] = failures.get(device_id, 0) + value
            for device_id, value in facts.metrics.device_reconnects.items():
                if worker_id in self._placements.worker_ids_for_device(device_id):
                    reconnects[device_id] = reconnects.get(device_id, 0) + value

            for event in facts.metrics.events:
                try:
                    owners = self._placements.worker_ids_for_device(event.object)
                except KeyError:
                    owners = [worker_id]
                if worker_id in owners:
                    events.append(event)

        sums["connect_failures"] = sum(failures.values())
        sums["reconnects"] = sum(reconnects.values())
        return MetricsSnapshot(
            counters=CounterSnapshot(**sums),
            device_connect_failures=failures,
            device_reconnects=reconnects,
            events=events,
        )
