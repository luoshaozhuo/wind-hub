"""基于 TaskPlacement 的多 Collector 低频聚合快照。"""

from __future__ import annotations

import asyncio
from typing import Any

from wind_hub_server.application.config.service import ConfigService
from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.task.placement import TaskPlacementRegistry


class CollectorStatusAggregator:
    """每个 Collector 只校验一次身份，再并发读取低频运行快照。"""

    def __init__(
        self,
        collectors: CollectorDirectory,
        placements: TaskPlacementRegistry,
        config: ConfigService,
    ) -> None:
        self._collectors = collectors
        self._placements = placements
        self._config = config

    async def snapshot(self) -> dict[str, Any]:
        """返回 MonitoringService 所需的完整 Collector 聚合快照。"""
        worker_ids = self._collectors.list_worker_ids()
        results = await asyncio.gather(
            *(self._worker_snapshot(worker_id) for worker_id in worker_ids),
            return_exceptions=True,
        )

        by_worker: dict[str, dict[str, Any]] = {}
        unavailable_workers: list[str] = []
        for worker_id, result in zip(worker_ids, results, strict=True):
            if isinstance(result, BaseException):
                unavailable_workers.append(worker_id)
                continue
            by_worker[worker_id] = result

        devices = self._aggregate_devices(by_worker)
        sinks = self._aggregate_sinks(by_worker)
        tasks = self._aggregate_tasks(by_worker)
        runtime = self._aggregate_runtime(by_worker, unavailable_workers, devices, sinks)
        metrics = self._aggregate_metrics(by_worker)

        return {
            "runtime_status": runtime,
            "metrics": metrics,
            "devices": devices,
            "sinks": sinks,
            "tasks": tasks,
        }

    async def _worker_snapshot(self, worker_id: str) -> dict[str, Any]:
        """校验一次 Collector 身份，然后并发读取本 Worker 的四类只读状态。"""
        collector = self._collectors.get(worker_id)
        status = await collector.config_status()
        reported_id = str(status.get("collector_id") or "")
        if reported_id != worker_id:
            raise RuntimeError(
                f"collector identity mismatch: expected={worker_id} "
                f"reported={reported_id or '<empty>'}"
            )
        runtime, metrics, devices, sinks, tasks = await asyncio.gather(
            collector.runtime_status(),
            collector.metrics_snapshot(),
            collector.list_devices(),
            collector.list_sinks(),
            collector.list_tasks(),
        )
        return {
            "runtime_status": runtime,
            "metrics": metrics,
            "devices": devices,
            "sinks": sinks,
            "tasks": tasks,
        }

    def _aggregate_runtime(
        self,
        by_worker: dict[str, dict[str, Any]],
        unavailable_workers: list[str],
        devices: list[dict[str, Any]],
        sinks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        acquisitions: list[dict[str, Any]] = []
        points_collected = points_routed = points_dropped = 0
        running = not unavailable_workers

        for worker_id, snapshot in by_worker.items():
            status = snapshot["runtime_status"]
            if not isinstance(status, dict):
                raise TypeError(
                    f"unexpected runtime_status payload from worker '{worker_id}': "
                    f"{type(status).__name__}"
                )
            assigned = set(self._placements.task_ids_for_worker(worker_id))
            running = running and bool(status.get("running"))
            points_collected += int(status.get("points_collected") or 0)
            points_routed += int(status.get("points_routed") or 0)
            points_dropped += int(status.get("points_dropped") or 0)
            acquisitions.extend(
                dict(item)
                for item in list(status.get("acquisitions") or [])
                if isinstance(item, dict)
                and str(item.get("task_id") or "") in assigned
            )

        return {
            "running": running,
            "device_count": len(self._config.current_config.devices.devices),
            "sink_count": len(self._config.current_config.sinks.sinks),
            "devices_connected": sum(1 for row in devices if bool(row.get("connected"))),
            "sinks_healthy": sum(1 for row in sinks if bool(row.get("healthy"))),
            "points_collected": points_collected,
            "points_routed": points_routed,
            "points_dropped": points_dropped,
            "acquisitions": acquisitions,
            "degraded": bool(unavailable_workers),
            "unavailable_workers": list(unavailable_workers),
        }

    def _aggregate_devices(
        self,
        by_worker: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        worker_devices: dict[str, dict[str, dict[str, Any]]] = {}
        for worker_id, snapshot in by_worker.items():
            raw = snapshot["devices"]
            if not isinstance(raw, list):
                raise TypeError(
                    f"unexpected list_devices payload from worker '{worker_id}': "
                    f"{type(raw).__name__}"
                )
            worker_devices[worker_id] = {
                str(row.get("device_id")): row
                for row in raw
                if isinstance(row, dict) and row.get("device_id") is not None
            }

        rows: list[dict[str, Any]] = []
        for cfg in self._config.current_config.devices.devices:
            owners = self._placements.worker_ids_for_device(cfg.device_id)
            states = [
                worker_devices.get(worker_id, {}).get(cfg.device_id)
                for worker_id in owners
            ]
            present = [row for row in states if row is not None]
            rows.append(
                {
                    "device_id": cfg.device_id,
                    "protocol": cfg.protocol,
                    "connected": (
                        bool(owners)
                        and len(present) == len(owners)
                        and all(bool(row.get("connected")) for row in present)
                    ),
                    "consecutive_failures": max(
                        (int(row.get("consecutive_failures") or 0) for row in present),
                        default=0,
                    ),
                    "last_error": (
                        next(
                            (
                                str(row.get("last_error"))
                                for row in present
                                if row.get("last_error")
                            ),
                            None,
                        )
                        if len(present) == len(owners)
                        else "collector unavailable"
                    ),
                }
            )
        return rows

    def _aggregate_sinks(
        self,
        by_worker: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        worker_sinks: dict[str, dict[str, dict[str, Any]]] = {}
        for worker_id, snapshot in by_worker.items():
            raw = snapshot["sinks"]
            if not isinstance(raw, list):
                raise TypeError(
                    f"unexpected list_sinks payload from worker '{worker_id}': "
                    f"{type(raw).__name__}"
                )
            worker_sinks[worker_id] = {
                str(row.get("name")): row
                for row in raw
                if isinstance(row, dict) and row.get("name") is not None
            }

        rows: list[dict[str, Any]] = []
        for cfg in self._config.current_config.sinks.sinks:
            owners = self._placements.worker_ids_for_sink(cfg.name)
            states = [
                worker_sinks.get(worker_id, {}).get(cfg.name)
                for worker_id in owners
            ]
            present = [row for row in states if row is not None]
            rows.append(
                {
                    "name": cfg.name,
                    "healthy": (
                        bool(owners)
                        and len(present) == len(owners)
                        and all(bool(row.get("healthy")) for row in present)
                    ),
                    "message": (
                        next(
                            (
                                str(row.get("message"))
                                for row in present
                                if row.get("message")
                            ),
                            None,
                        )
                        if len(present) == len(owners)
                        else "collector unavailable"
                    ),
                    "queue_depth": sum(
                        int(row.get("queue_depth") or 0) for row in present
                    ),
                }
            )
        return rows

    def _aggregate_tasks(
        self,
        by_worker: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """按 placement 过滤各 Collector 的 Task 聚合状态。"""
        rows: list[dict[str, Any]] = []
        for worker_id, snapshot in by_worker.items():
            raw = snapshot["tasks"]
            if not isinstance(raw, list):
                raise TypeError(
                    f"unexpected list_tasks payload from worker '{worker_id}': "
                    f"{type(raw).__name__}"
                )
            assigned = set(self._placements.task_ids_for_worker(worker_id))
            rows.extend(
                {**row, "assigned_worker_id": worker_id}
                for row in raw
                if isinstance(row, dict)
                and str(row.get("task_id") or "") in assigned
            )
        return rows

    def _aggregate_metrics(
        self,
        by_worker: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        counter_names = (
            "points_total",
            "points_bad",
            "acquisition_runs",
            "acquisition_failures",
            "acquisition_partial",
            "missed_cycles",
            "poll_overruns",
            "connect_failures",
            "reconnects",
        )
        counters = {name: 0 for name in counter_names}
        failures: dict[str, int] = {}
        reconnects: dict[str, int] = {}
        events: list[dict[str, Any]] = []

        for worker_id, snapshot in by_worker.items():
            metrics = snapshot["metrics"]
            if not isinstance(metrics, dict):
                raise TypeError(
                    f"unexpected metrics_snapshot payload from worker '{worker_id}': "
                    f"{type(metrics).__name__}"
                )
            raw_counters = metrics.get("counters")
            if isinstance(raw_counters, dict):
                for name in counter_names:
                    counters[name] += int(raw_counters.get(name) or 0)

            for key, target in (
                ("device_connect_failures", failures),
                ("device_reconnects", reconnects),
            ):
                values = metrics.get(key)
                if not isinstance(values, dict):
                    continue
                for device_id, value in values.items():
                    if worker_id in self._placements.worker_ids_for_device(str(device_id)):
                        target[str(device_id)] = target.get(str(device_id), 0) + int(value)

            for item in list(metrics.get("events") or []):
                if not isinstance(item, dict):
                    continue
                object_id = str(item.get("object") or "")
                try:
                    owners = self._placements.worker_ids_for_device(object_id)
                except KeyError:
                    owners = [worker_id]
                if worker_id in owners:
                    events.append(dict(item))

        counters["connect_failures"] = sum(failures.values())
        counters["reconnects"] = sum(reconnects.values())
        return {
            "counters": counters,
            "device_connect_failures": failures,
            "device_reconnects": reconnects,
            "events": events,
        }
