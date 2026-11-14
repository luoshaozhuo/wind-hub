"""Port for external, authenticated field-control fencing evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RecoveryProof:
    token_id: str
    device_id: str
    revision: str
    old_instance: str
    new_instance: str
    fencing_epoch: int
    expires_at: datetime


class RecoveryAuthority(Protocol):
    async def verify_and_fence(
        self,
        *,
        device_id: str,
        revision: str,
        caller_id: str,
        request_id: str,
        attestation: bytes,
    ) -> RecoveryProof: ...
