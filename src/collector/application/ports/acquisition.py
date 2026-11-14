"""Collector sampling and telemetry output ports."""

from __future__ import annotations

from typing import Protocol

from core.application import ProtocolSample


class MetricsSubjectPort(Protocol):
    def update_site(self, site_id: str) -> None: ...


class SampleSink(Protocol):
    async def publish(self, device_id: str, samples: tuple[ProtocolSample, ...]) -> None: ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def update_site(self, site_id: str) -> None: ...
