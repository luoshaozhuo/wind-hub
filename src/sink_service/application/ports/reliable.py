"""Reliable source message boundary independent of JetStream client objects."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class SourceMetadata:
    num_pending: int
    num_delivered: int
    timestamp: datetime


class ReliableMessagePort(Protocol):
    @property
    def data(self) -> bytes: ...
    @property
    def subject(self) -> str: ...
    @property
    def metadata(self) -> SourceMetadata: ...

    async def ack(self, timeout: float) -> None: ...
    async def retry(self, delay: float) -> None: ...
    async def extend(self) -> None: ...
