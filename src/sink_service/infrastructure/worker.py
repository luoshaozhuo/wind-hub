"""Sink data worker subprocess with a dedicated asyncio event loop."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Literal

from sink_service.application.worker_coordinator import WorkerCoordinator
from sink_service.infrastructure.ipc import ControlReply, ControlRequest, read_frame, write_frame
from sink_service.infrastructure.persistence.config_repository import GenerationStore
from sink_service.infrastructure.realtime_runtime import RealtimeFactory
from sink_service.infrastructure.reliable_runtime import ReliableFactory

logger = logging.getLogger(__name__)


async def run_worker(role: str, root: Path, service_file: str, socket_path: str, active_revision: str) -> None:
    """Run transport and persistence adapters around the Application coordinator."""
    if role not in ("realtime", "reliable"):
        raise ValueError("invalid worker process role")
    store = GenerationStore(root)
    snapshot = store.load_generation(active_revision, service_file)
    factory = RealtimeFactory() if role == "realtime" else ReliableFactory()
    coordinator = WorkerCoordinator(role, snapshot, factory)
    await coordinator.start()
    reader: asyncio.StreamReader | None = None
    writer: asyncio.StreamWriter | None = None
    try:
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
        while True:
            request = ControlRequest.from_wire(await read_frame(reader))
            status: Literal["APPLIED", "PENDING", "FAILED", "UNKNOWN"] = "APPLIED"
            evidence: dict[str, str] = {}
            try:
                if request.operation == "SHUTDOWN":
                    await coordinator.shutdown()
                elif request.operation in {"APPLY", "START", "STOP"}:
                    updated = store.load(service_file)
                    if updated.revision != request.generation:
                        raise ValueError("generation mismatch")
                    status, ids = await coordinator.apply(updated)
                    evidence["applied_ids"] = json.dumps(ids)
                elif request.operation == "GET_STATUS":
                    state = coordinator.status()
                    evidence["active_generation"] = str(state["active_generation"])
                    evidence["pid"] = str(os.getpid())
                    evidence["deliveries"] = json.dumps(state["deliveries"], separators=(",", ":"))
                    evidence["inspections"] = json.dumps(
                        state["inspections"], separators=(",", ":")
                    )
                elif request.operation == "GET_METRICS":
                    ingress = (
                        factory.ingress_counters() if isinstance(factory, RealtimeFactory) else {}
                    )
                    measures = coordinator.metrics(ingress)
                    evidence["active_generation"] = str(measures["active_generation"])
                    for name in (
                        "delivery_latency_p99",
                        "counters",
                        "delivery_counters",
                        "latency_bins",
                    ):
                        evidence[name] = json.dumps(measures[name], separators=(",", ":"))
                elif request.operation == "DIAGNOSE":
                    evidence["details"] = json.dumps(coordinator.diagnose(request.delivery_id))
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
        await coordinator.shutdown()
        if writer is not None:
            writer.close()
            await writer.wait_closed()


def worker_main(role: str, root: str, service_file: str, socket_path: str, active_revision: str) -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_worker(role, Path(root), service_file, socket_path, active_revision))
