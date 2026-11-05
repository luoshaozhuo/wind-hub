"""Read-only worker snapshot cache for Main."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from sink_service.application.delivery_definition import delivery_definitions
from sink_service.application.delivery_registry import DeliveryView
from sink_service.domain.config import ValidatedSnapshot
from sink_service.infrastructure.supervisor import ROLES, WorkerSupervisor


class WorkerRegistryView:
    """Read-only snapshots supplied by worker IPC."""

    def __init__(self, supervisor: WorkerSupervisor, snapshot: ValidatedSnapshot) -> None:
        self.supervisor = supervisor
        self.snapshot = snapshot
        self.views: dict[str, DeliveryView] = {}
        self.worker_health: dict[str, str] = {role: "UNKNOWN" for role in ROLES}
        self.reset(snapshot)

    def reset(self, snapshot: ValidatedSnapshot) -> None:
        self.snapshot = snapshot
        previous = self.views
        self.views = {
            entry.delivery_id: DeliveryView(
                delivery_id=entry.delivery_id,
                channel=entry.channel,
                sink_id=entry.sink_id,
                process_role=entry.channel,
                desired_state=entry.desired_state,
                actual_state=previous[entry.delivery_id].actual_state if entry.delivery_id in previous else "UNKNOWN",
                revision=previous[entry.delivery_id].revision if entry.delivery_id in previous else "",
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
        return {"delivery": self.get(delivery_id).actual_state}

    def counters(self) -> dict[str, float]:
        return {}

    async def refresh(self) -> None:
        replies = await asyncio.gather(
            *(self.supervisor.request(role, "GET_STATUS", self.snapshot.revision)
              for role in ROLES),
            return_exceptions=True,
        )
        for role, reply in zip(ROLES, replies, strict=True):
            try:
                if isinstance(reply, BaseException) or reply.status != "APPLIED":
                    raise ValueError("worker not available")
                reports = json.loads(reply.evidence["deliveries"])
                self.worker_health[role] = "RUNNING"
                by_id = {entry["delivery_id"]: entry for entry in reports}
                for key, old in tuple(self.views.items()):
                    if old.channel == role:
                        if key not in by_id:
                            raise ValueError("worker did not report Delivery")
                        self.views[key] = DeliveryView(**by_id[key])
            except (ValueError, KeyError, TypeError):
                self.worker_health[role] = "UNKNOWN"
                for key, old in tuple(self.views.items()):
                    if old.channel == role:
                        values = asdict(old)
                        values["actual_state"] = "UNKNOWN"
                        values["last_error"] = "worker unavailable"
                        self.views[key] = DeliveryView(**values)

    async def apply_configuration(self, snapshot: ValidatedSnapshot) -> tuple[str, ...]:
        self.reset(snapshot)
        applied = await self.supervisor.apply(snapshot.revision)
        await self.refresh()
        return applied
