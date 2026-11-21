"""Collector sampling and telemetry output ports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from core.application import ProtocolSample
from core.domain import BusinessPoint, PointTable


class MetricsSubjectPort(Protocol):
    def update_site(self, site_id: str) -> None: ...


class SampleSink(Protocol):
    async def publish(
        self,
        device_id: str,
        samples: tuple[ProtocolSample, ...],
        *,
        device_type: str,
        point_table_id: str,
        point_table: PointTable,
        business_points: Mapping[str, BusinessPoint],
    ) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def update_site(self, site_id: str) -> None: ...
