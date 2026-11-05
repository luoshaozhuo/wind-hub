"""Read-only worker snapshot cache for Main."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from datetime import datetime

from sink_service.application.delivery_definition import delivery_definitions
from sink_service.application.delivery_registry import DeliveryView
from sink_service.domain.config import ValidatedSnapshot
from sink_service.infrastructure.supervisor import ROLES, WorkerSupervisor


class WorkerRegistryView:
    """Read-only snapshots supplied by worker IPC."""

    def __init__(self, supervisor: WorkerSupervisor, snapshot: ValidatedSnapshot) -> None:
        self._lock = asyncio.Lock()
        self.supervisor = supervisor
        self.snapshot = snapshot
        self.views: dict[str, DeliveryView] = {}
        self.inspections: dict[str, dict[str, str]] = {}
        self.worker_health: dict[str, str] = {role: "UNKNOWN" for role in ROLES}
        self.worker_pid: dict[str, str] = {}
        self.epoch_changed = False
        self.reset(snapshot)

    def reset(self, snapshot: ValidatedSnapshot) -> None:
        self.snapshot = snapshot
        self.inspections.clear()
        self.views = {
            entry.delivery_id: DeliveryView(
                delivery_id=entry.delivery_id,
                channel=entry.channel,
                sink_id=entry.sink_id,
                process_role=entry.channel,
                desired_state=entry.desired_state,
                actual_state="UNKNOWN",
                revision="",
                last_error="worker evidence pending",
            )
            for entry in delivery_definitions(snapshot)
        }

    def list(self, channel: str = "") -> tuple[DeliveryView, ...]:
        return tuple(
            view for _, view in sorted(self.views.items())
            if not channel or view.channel == channel
        )

    def get(self, delivery_id: str) -> DeliveryView:
        return self.views[delivery_id]

    def inspect(self, delivery_id: str) -> dict[str, str]:
        return dict(self.inspections.get(delivery_id, {}))

    async def refresh(self) -> None:
        async with self._lock:
            await self._refresh_locked()

    async def _refresh_locked(self) -> None:
        replies = await asyncio.gather(
            *(self.supervisor.request(role, "GET_STATUS", self.snapshot.revision)
              for role in ROLES),
            return_exceptions=True,
        )
        for role, reply in zip(ROLES, replies, strict=True):
            try:
                if isinstance(reply, BaseException) or reply.status != "APPLIED":
                    raise ValueError("worker not available")
                if reply.evidence["active_generation"] != self.snapshot.revision:
                    raise ValueError("worker active generation is stale")
                current_pid = reply.evidence["pid"]
                previous_pid = self.worker_pid.get(role)
                if previous_pid is not None and previous_pid != current_pid:
                    self.epoch_changed = True
                self.worker_pid[role] = current_pid
                reports = json.loads(reply.evidence["deliveries"])
                inspected = json.loads(reply.evidence["inspections"])
                self.worker_health[role] = "RUNNING"
                by_id = {entry["delivery_id"]: entry for entry in reports}
                for key, old in tuple(self.views.items()):
                    if old.channel == role:
                        if key not in by_id:
                            raise ValueError("worker did not report Delivery")
                        candidate = DeliveryView(**by_id[key])
                        if candidate.revision != self.snapshot.revision:
                            raise ValueError("worker reports stale configuration generation")
                        self.views[key] = candidate
                        self.inspections[key] = inspected.get(key, {})
            except (ValueError, KeyError, TypeError):
                self.worker_health[role] = "UNKNOWN"
                for key, old in tuple(self.views.items()):
                    if old.channel == role:
                        values = asdict(old)
                        values["actual_state"] = "UNKNOWN"
                        values["last_error"] = "worker unavailable"
                        self.views[key] = DeliveryView(**values)
                        self.inspections.pop(key, None)

    async def apply_configuration(self, snapshot: ValidatedSnapshot) -> tuple[str, ...]:
        async with self._lock:
            self.reset(snapshot)
            applied = await self.supervisor.apply(snapshot.revision)
            await self._refresh_locked()
            if any(state != "RUNNING" for state in self.worker_health.values()):
                raise RuntimeError("one or more worker generations are not converged")
            return applied

    async def collect_metrics(
        self,
    ) -> tuple[
        dict[str, float],
        dict[str, dict[str, float]],
        tuple[tuple[datetime, float], ...],
        dict[str, tuple[tuple[datetime, float], ...]],
        bool,
    ] | None:
        """Aggregate both worker reports only when both generations are valid."""
        replies = await asyncio.gather(
            *(
                self.supervisor.request(role, "GET_METRICS", self.snapshot.revision)
                for role in ROLES
            ),
            return_exceptions=True,
        )
        aggregate: dict[str, float] = {}
        deliveries: dict[str, dict[str, float]] = {}
        latencies: list[tuple[datetime, float]] = []
        delivery_latencies: dict[str, tuple[tuple[datetime, float], ...]] = {}
        incomplete = False
        for reply in replies:
            if isinstance(reply, BaseException) or reply.status != "APPLIED":
                return None
            try:
                if reply.evidence["active_generation"] != self.snapshot.revision:
                    return None
                values = json.loads(reply.evidence["counters"])
                delivery_values = json.loads(reply.evidence["delivery_counters"])
                for name, value in values.items():
                    aggregate[name] = aggregate.get(name, 0.0) + float(value)
                for identifier, values in delivery_values.items():
                    if identifier in deliveries:
                        return None
                    deliveries[identifier] = {
                        name: float(value) for name, value in values.items()
                    }
                for identifier, values in json.loads(
                    reply.evidence.get("delivery_latencies", "{}")
                ).items():
                    delivery_latencies[identifier] = tuple(
                        (datetime.fromisoformat(stamp), float(value))
                        for stamp, value in values
                    )
                if reply.evidence.get("latency_incomplete") == "true":
                    incomplete = True
                for timestamp, value in json.loads(
                    reply.evidence.get("latencies", "[]")
                ):
                    latencies.append((datetime.fromisoformat(timestamp), float(value)))
            except (KeyError, TypeError, ValueError, OverflowError):
                return None
        return aggregate, deliveries, tuple(latencies), delivery_latencies, incomplete
