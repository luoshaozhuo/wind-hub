"""Sink data worker subprocess with a dedicated asyncio event loop."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from dataclasses import asdict
from datetime import UTC, datetime
from math import ceil
from pathlib import Path
from typing import Literal

from sink_service.application.delivery_registry import DeliveryRegistry
from sink_service.application.latency_window import LatencyWindow
from sink_service.infrastructure.config_repository import GenerationStore
from sink_service.infrastructure.ipc import ControlReply, ControlRequest, read_frame, write_frame
from sink_service.infrastructure.realtime_runtime import RealtimeFactory
from sink_service.infrastructure.reliable_runtime import ReliableFactory

logger = logging.getLogger(__name__)


async def run_worker(role: str, root: Path, service_file: str, socket_path: str) -> None:
    if role not in ("realtime", "reliable"):
        raise ValueError("invalid worker process role")
    store = GenerationStore(root)
    snapshot = store.load(service_file)
    registry = DeliveryRegistry(role, snapshot)
    factory = RealtimeFactory() if role == "realtime" else ReliableFactory()
    await registry.apply_configuration(snapshot, factory)
    reader, writer = await asyncio.open_unix_connection(socket_path)
    await write_frame(
        writer,
        ControlReply(
            request_id="BOOT",
            generation=snapshot.revision,
            status="APPLIED",
            evidence={"role": role},
        ).to_wire(),
    )

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(
                snapshot.service["runtime"].get("watchdog_interval_ms", 5000) / 1000
            )
            try:
                await registry.reconcile_all()
            except Exception:
                logger.exception("worker watchdog reconciliation failed")

    lag_samples: deque[tuple[float, float]] = deque(maxlen=60000)

    async def monitor_loop_delay() -> None:
        loop = asyncio.get_running_loop()
        target = loop.time() + 0.1
        while True:
            await asyncio.sleep(max(0.0, target - loop.time()))
            actual = loop.time()
            lag_samples.append((actual, max(0.0, (actual - target) * 1000)))
            target = actual + 0.1

    recovery = asyncio.create_task(watchdog())
    lag_monitor = asyncio.create_task(monitor_loop_delay())
    try:
        while True:
            request = ControlRequest.from_wire(await read_frame(reader))
            status: Literal["APPLIED", "PENDING", "FAILED", "UNKNOWN"] = "APPLIED"
            evidence: dict[str, str] = {}
            try:
                if request.operation == "SHUTDOWN":
                    recovery.cancel()
                    await asyncio.gather(recovery, return_exceptions=True)
                    await registry.shutdown()
                elif request.operation in {"APPLY", "START", "STOP"}:
                    updated = store.load(service_file)
                    if updated.revision != request.generation:
                        raise ValueError("generation mismatch")
                    if updated.service != snapshot.service:
                        raise ValueError("RESTART_REQUIRED")
                    reconciled = await registry.apply_configuration(updated, factory)
                    snapshot = updated
                    views = registry.list()
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
                    evidence["applied_ids"] = json.dumps(applied)
                    if any(item.delivery_id not in applied for item in views):
                        status = "PENDING"
                elif request.operation == "GET_STATUS":
                    evidence["active_generation"] = snapshot.revision
                    evidence["pid"] = str(os.getpid())
                    views = registry.list()
                    evidence["deliveries"] = json.dumps(
                        [asdict(item) for item in views], separators=(",", ":")
                    )
                    evidence["inspections"] = json.dumps(
                        {item.delivery_id: registry.inspect(item.delivery_id) for item in views},
                        separators=(",", ":"),
                    )
                elif request.operation == "GET_METRICS":
                    evidence["active_generation"] = snapshot.revision
                    counters = registry.counters()
                    if isinstance(factory, RealtimeFactory):
                        counters.update(factory.ingress_counters())
                    lag_cutoff = asyncio.get_running_loop().time() - (
                        snapshot.service["metrics"]["metrics_window_ms"] / 1000
                    )
                    lag_values = sorted(delay for stamp, delay in lag_samples if stamp > lag_cutoff)
                    if lag_values:
                        counters[f"{role}_event_loop_lag_ms_p99"] = lag_values[
                            ceil(len(lag_values) * 0.99) - 1
                        ]
                    deliveries, histories = registry.delivery_observations()
                    instant = datetime.now(UTC)
                    window_ms = snapshot.service["metrics"]["metrics_window_ms"]
                    p99: dict[str, float] = {}
                    for identifier, snapshots in histories.items():
                        value = LatencyWindow.percentile(
                            snapshots, now=instant, window_ms=window_ms
                        )
                        if value is not None:
                            p99[identifier] = value
                    evidence["delivery_latency_p99"] = json.dumps(p99)
                    evidence["counters"] = json.dumps(counters)
                    evidence["delivery_counters"] = json.dumps(deliveries, separators=(",", ":"))
                    bins = LatencyWindow.window_bins(
                        registry.latency_histograms(), now=instant, window_ms=window_ms
                    )
                    evidence["latency_bins"] = json.dumps(
                        sorted(bins.items()), separators=(",", ":")
                    )
                elif request.operation == "DIAGNOSE":
                    evidence["details"] = json.dumps(
                        registry.inspect(request.delivery_id) if request.delivery_id else {}
                    )
            except Exception as exc:
                logger.exception("worker command failed")
                status = "FAILED"
                evidence["error"] = str(exc)
            await write_frame(
                writer,
                ControlReply(
                    request_id=request.request_id,
                    generation=request.generation,
                    status=status,
                    evidence=evidence,
                ).to_wire(),
            )
            if request.operation == "SHUTDOWN":
                break
    finally:
        recovery.cancel()
        lag_monitor.cancel()
        await asyncio.gather(recovery, lag_monitor, return_exceptions=True)
        await registry.shutdown()
        writer.close()
        await writer.wait_closed()


def worker_main(role: str, root: str, service_file: str, socket_path: str) -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(role, Path(root), service_file, socket_path))
