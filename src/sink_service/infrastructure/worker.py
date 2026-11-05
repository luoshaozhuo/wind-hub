"""Sink data worker subprocess with a dedicated asyncio event loop."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import asdict
from pathlib import Path

from sink_service.application.delivery_registry import DeliveryRegistry
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
    await write_frame(writer, ControlReply(
        request_id="BOOT", generation=snapshot.revision,
        status="APPLIED", evidence={"role": role},
    ).to_wire())
    async def watchdog() -> None:
        while True:
            await asyncio.sleep(
                snapshot.service["runtime"].get("watchdog_interval_ms", 5000) / 1000
            )
            try:
                await registry.reconcile_all()
            except Exception:
                logger.exception("worker watchdog reconciliation failed")

    recovery = asyncio.create_task(watchdog())
    try:
        while True:
            request = ControlRequest.from_wire(await read_frame(reader))
            status = "APPLIED"
            evidence: dict[str, str] = {}
            try:
                if request.operation == "SHUTDOWN":
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
                    applied = tuple(sorted(set(reconciled) | {
                        item.delivery_id for item in views
                        if item.revision == updated.revision
                        and item.actual_state == (
                            "RUNNING" if item.desired_state == "START" else "STOPPED"
                        ) and not item.last_error
                    }))
                    evidence["applied_ids"] = json.dumps(applied)
                    if any(item.delivery_id not in applied for item in views):
                        status = "PENDING"
                elif request.operation == "GET_STATUS":
                    evidence["active_generation"] = snapshot.revision
                    evidence["deliveries"] = json.dumps(
                        [asdict(item) for item in registry.list()]
                    )
                elif request.operation == "GET_METRICS":
                    evidence["active_generation"] = snapshot.revision
                    counters = registry.counters()
                    if isinstance(factory, RealtimeFactory):
                        counters.update(factory.ingress_counters())
                    deliveries, _, _ = registry.delivery_observations()
                    evidence["counters"] = json.dumps(counters)
                    evidence["delivery_counters"] = json.dumps(
                        deliveries, separators=(",", ":")
                    )
                    latencies = registry.latency_observations()
                    # Never fabricate a P99 from a truncated history.
                    if len(latencies) <= 512 and registry.latency_overflow_at() is None:
                        evidence["latencies"] = json.dumps([
                            [stamp.isoformat(), value]
                            for stamp, value in latencies
                        ], separators=(",", ":"))
                    else:
                        evidence["latency_incomplete"] = "true"
                elif request.operation == "DIAGNOSE":
                    evidence["details"] = json.dumps(
                        registry.inspect(request.delivery_id)
                        if request.delivery_id else {}
                    )
            except Exception as exc:
                logger.exception("worker command failed")
                status = "FAILED"
                evidence["error"] = str(exc)
            await write_frame(writer, ControlReply(
                request_id=request.request_id, generation=request.generation,
                status=status, evidence=evidence,
            ).to_wire())
            if request.operation == "SHUTDOWN":
                break
    finally:
        recovery.cancel()
        await asyncio.gather(recovery, return_exceptions=True)
        await registry.shutdown()
        writer.close()
        await writer.wait_closed()


def worker_main(role: str, root: str, service_file: str, socket_path: str) -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(role, Path(root), service_file, socket_path))
