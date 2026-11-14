"""Interfaces for realtime source ingress and shared output ownership."""

from __future__ import annotations

from typing import Protocol

from sink_service.application.output_pool import _QuotaWriter
from sink_service.application.realtime import RealtimeEngine
from sink_service.domain.config import ValidatedSnapshot


class RealtimeIngressPort(Protocol):
    @property
    def connected(self) -> bool: ...

    async def add(self, delivery_id: str, engine: RealtimeEngine) -> None: ...

    async def remove(self, delivery_id: str) -> None: ...


class SinkOutputPoolPort(Protocol):
    async def acquire(self, sink_id: str, snapshot: ValidatedSnapshot) -> _QuotaWriter: ...

    async def release(self, sink_id: str) -> None: ...
