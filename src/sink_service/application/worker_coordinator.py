"""Application ownership of Sink worker reconciliation, watchdog and observations."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from math import ceil
from typing import Literal

from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.application.delivery_registry import DeliveryPort, DeliveryRegistry
from sink_service.application.latency_window import LatencyWindow
from sink_service.domain.config import ValidatedSnapshot

DeliveryFactory = Callable[[DeliveryDefinition, ValidatedSnapshot], DeliveryPort]


class WorkerCoordinator:
    """No IPC, file I/O or concrete transport dependencies."""

    def __init__(self, role: str, snapshot: ValidatedSnapshot, factory: DeliveryFactory) -> None:
        self.role = role
        self.snapshot = snapshot
        self.registry = DeliveryRegistry(role, snapshot)
        self.factory = factory
        self._watchdog: asyncio.Task[None] | None = None
        self._monitor: asyncio.Task[None] | None = None
        self._lag_samples: deque[tuple[float, float]] = deque(maxlen=60000)
        self._closed = False

    async def start(self) -> None:
        await self.registry.apply_configuration(self.snapshot, self.factory)
        self._watchdog = asyncio.create_task(self._reconcile_loop())
        self._monitor = asyncio.create_task(self._monitor_loop_delay())

    async def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._watchdog, self._monitor):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self._watchdog, self._monitor) if task is not None),
            return_exceptions=True,
        )
        self._watchdog = None
        self._monitor = None
        await self.registry.shutdown()

    async def _reconcile_loop(self) -> None:
        while True:
            await asyncio.sleep(
                self.snapshot.service["runtime"].get("watchdog_interval_ms", 5000) / 1000
            )
            try:
                await self.registry.reconcile_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                import logging

                logging.getLogger(__name__).exception("worker watchdog reconciliation failed")

    async def _monitor_loop_delay(self) -> None:
        loop = asyncio.get_running_loop()
        target = loop.time() + 0.1
        while True:
            await asyncio.sleep(max(0.0, target - loop.time()))
            actual = loop.time()
            self._lag_samples.append((actual, max(0.0, (actual - target) * 1000)))
            target = actual + 0.1

    async def apply(
        self, updated: ValidatedSnapshot
    ) -> tuple[Literal["APPLIED", "PENDING"], tuple[str, ...]]:
        if updated.service != self.snapshot.service:
            raise ValueError("RESTART_REQUIRED")
        reconciled = await self.registry.apply_configuration(updated, self.factory)
        self.snapshot = updated
        views = self.registry.list()
        applied = tuple(
            sorted(
                set(reconciled)
                | {
                    item.delivery_id
                    for item in views
                    if item.revision == updated.revision
                    and item.actual_state
                    == ("RUNNING" if item.desired_state == "START" else "STOPPED")
                    and not item.last_error
                }
            )
        )
        status: Literal["APPLIED", "PENDING"] = (
            "PENDING" if any(item.delivery_id not in applied for item in views) else "APPLIED"
        )
        return status, applied

    def status(self) -> dict[str, object]:
        views = self.registry.list()
        return {
            "active_generation": self.snapshot.revision,
            "deliveries": [asdict(item) for item in views],
            "inspections": {
                item.delivery_id: self.registry.inspect(item.delivery_id) for item in views
            },
        }

    def metrics(self, ingress_counters: dict[str, float]) -> dict[str, object]:
        counters = self.registry.counters()
        counters.update(ingress_counters)
        loop = asyncio.get_running_loop()
        cutoff = loop.time() - self.snapshot.service["metrics"]["metrics_window_ms"] / 1000
        values = sorted(delay for stamp, delay in self._lag_samples if stamp > cutoff)
        if values:
            counters[f"{self.role}_event_loop_lag_ms_p99"] = values[ceil(len(values) * 0.99) - 1]
        deliveries, histories = self.registry.delivery_observations()
        instant = datetime.now(UTC)
        window_ms = self.snapshot.service["metrics"]["metrics_window_ms"]
        p99: dict[str, float] = {}
        for identifier, snapshots in histories.items():
            value = LatencyWindow.percentile(snapshots, now=instant, window_ms=window_ms)
            if value is not None:
                p99[identifier] = value
        bins = LatencyWindow.window_bins(
            self.registry.latency_histograms(), now=instant, window_ms=window_ms
        )
        return {
            "active_generation": self.snapshot.revision,
            "delivery_latency_p99": p99,
            "counters": counters,
            "delivery_counters": deliveries,
            "latency_bins": sorted(bins.items()),
        }

    def diagnose(self, delivery_id: str) -> dict[str, str]:
        return self.registry.inspect(delivery_id) if delivery_id else {}
