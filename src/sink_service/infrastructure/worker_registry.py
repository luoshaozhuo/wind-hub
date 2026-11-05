"""Read-only worker snapshot cache for Main."""

from __future__ import annotations

from sink_service.application.delivery_registry import DeliveryView

from sink_service.application.delivery_definition import delivery_definitions
from sink_service.domain.config import ValidatedSnapshot


class WorkerRegistryView:
    """Read-only snapshots supplied by worker IPC."""

    def __init__(self, snapshot: ValidatedSnapshot) -> None:
        self.views: dict[str, DeliveryView] = {}
        self.reset(snapshot)

    def reset(self, snapshot: ValidatedSnapshot) -> None:
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
        return {"delivery": self.get(delivery_id).actual_state}

    def counters(self) -> dict[str, float]:
        return {}
