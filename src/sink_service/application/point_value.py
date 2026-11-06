"""Sink Service 业务专属的采集任务与时效性元数据。"""

from __future__ import annotations

from dataclasses import dataclass

from core.domain.point_value import PointValue


@dataclass(frozen=True, slots=True)
class SinkPointValue(PointValue):
    """共享点值之外的 Sink 传递元数据。"""

    task_id: str | None = None
    max_staleness_ms: int | None = None
