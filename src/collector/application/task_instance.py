"""One immutable acquisition task instance for each enabled Collector device."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class TaskInstanceState(str, Enum):
    """Actual acquisition handle state, independent of persisted intent."""

    RUNNING = "running"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class CollectionTaskInstance:
    """Runtime acquisition definition for one device."""

    instance_id: str
    task_id: str
    device_id: str
    point_group: str
    interval: float | None
