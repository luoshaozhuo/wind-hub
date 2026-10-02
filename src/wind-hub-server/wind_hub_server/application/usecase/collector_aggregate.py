"""基于 TaskAssignment 的多 Collector 聚合读模型。"""

from __future__ import annotations

import asyncio
from typing import Any

from wind_hub_server.application.port.collector_directory import CollectorDirectory
from wind_hub_server.application.usecase.config import ConfigUseCase
from wind_hub_server.application.usecase.task_assignment import TaskAssignmentUseCase


class CollectorAggregateUseCase:
    """按 assignment 边界聚合多个 Collector 的运行态。"""

    def __init__(
        self,
        collectors: CollectorDirectory,
        assignments: TaskAssignmentUseCase,
        config: ConfigUseCase,
    ) -> None:
        self._collectors = collectors
        self._assignments = assignments
        self._config = config

    async def runtime_status(self) -> dict[str, Any]:
        worker_ids = self._collectors.list_worker_ids()
        statuses = await asyncio.gather(
            *(self._collectors.get(worker_id).runtime_status() for worker_id in worker_ids)
        )
        acquisitions: list[dict[str, Any]] = []
        points_collected = points_routed = points_dropped = 0
        running = True
        for worker_id, status in zip(worker_ids, statuses, strict=True):
            assigned = set(self._assignments.task_ids_for_worker(worker_id))
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

        devices = await self.list_devices()
        sinks = await self.list_sinks()
        return {
            "running": running,
            "device_count": len(self._config.current_config.devices.devices),
            "sink_count": len(self._config.current_config.system.sinks),
            "devices_connected": sum(1 for row in devices if bool(row.get("connected"))),
            "sinks_healthy": sum(1 for row in sinks if bool(row.get("healthy"))),
            "points_collected": points_collected,
            "points_routed": points_routed,
            "points_dropped": points_dropped,
            "acquisitions": acquisitions,
        }

    async def list_devices(self) -> list[dict[str, Any]]:
        worker_ids = self._collectors.list_worker_ids()
        results = await asyncio.gather(
            *(self._collectors.get(worker_id).list_devices() for worker_id in worker_ids)
        )
        by_worker = {
            worker_id: {
                str(row.get("device_id")): row
                for row in rows
                if row.get("device_id") is not None
            }
            for worker_id, rows in zip(worker_ids, results, strict=True)
        }
        rows: list[dict[str, Any]] = []
        for cfg in self._config.current_config.devices.devices:
            owners = self._assignments.worker_ids_for_device(cfg.device_id)
            states = [
                by_worker.get(worker_id, {}).get(cfg.device_id)
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
                    "last_error": next(
                        (
                            str(row.get("last_error"))
                            for row in present
                            if row.get("last_error")
                        ),
                        None,
                    ),
                }
            )
        return rows

    async def list_sinks(self) -> list[dict[str, Any]]:
        worker_ids = self._collectors.list_worker_ids()
        results = await asyncio.gather(
            *(self._collectors.get(worker_id).list_sinks() for worker_id in worker_ids)
        )
        by_worker = {
            worker_id: {
                str(row.get("name")): row
                for row in rows
                if row.get("name") is not None
            }
            for worker_id, rows in zip(worker_ids, results, strict=True)
        }
        rows: list[dict[str, Any]] = []
        for cfg in self._config.current_config.system.sinks:
            owners = self._assignments.worker_ids_for_sink(cfg.name)
            states = [by_worker.get(worker_id, {}).get(cfg.name) for worker_id in owners]
            present = [row for row in states if row is not None]
            rows.append(
                {
                    "name": cfg.name,
                    "healthy": (
                        bool(owners)
                        and len(present) == len(owners)
                        and all(bool(row.get("healthy")) for row in present)
                    ),
                    "message": next(
                        (
                            str(row.get("message"))
                            for row in present
                            if row.get("message")
                        ),
                        None,
                    ),
                    "queue_depth": sum(
                        int(row.get("queue_depth") or 0) for row in present
                    ),
                }
            )
        return rows

    async def metrics_snapshot(self) -> dict[str, Any]:
        worker_ids = self._collectors.list_worker_ids()
        snapshots = await asyncio.gather(
            *(self._collectors.get(worker_id).metrics_snapshot() for worker_id in worker_ids)
        )
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

        for worker_id, snapshot in zip(worker_ids, snapshots, strict=True):
            raw_counters = snapshot.get("counters")
            if isinstance(raw_counters, dict):
                for name in counter_names:
                    counters[name] += int(raw_counters.get(name) or 0)

            for key, target in (
                ("device_connect_failures", failures),
                ("device_reconnects", reconnects),
            ):
                values = snapshot.get(key)
                if not isinstance(values, dict):
                    continue
                for device_id, value in values.items():
                    if worker_id in self._assignments.worker_ids_for_device(str(device_id)):
                        target[str(device_id)] = target.get(str(device_id), 0) + int(value)

            for item in list(snapshot.get("events") or []):
                if not isinstance(item, dict):
                    continue
                object_id = str(item.get("object") or "")
                try:
                    owners = self._assignments.worker_ids_for_device(object_id)
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
