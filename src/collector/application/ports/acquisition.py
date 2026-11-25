"""Collector sampling and telemetry output ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from core.application import ProtocolSample


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
        task_id: str,
        device_config_id: str,
        sample_time: datetime,
        expected_point_ids: tuple[str, ...],
    ) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def update_site(self, site_id: str) -> None: ...
