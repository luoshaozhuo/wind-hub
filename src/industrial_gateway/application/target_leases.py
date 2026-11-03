"""Gateway-local atomic target leasing with one-time fencing tokens."""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TargetLease:
    owner_id: str
    token: str
    expires_at: float


class TargetLeaseRegistry:
    """A Gateway process is the sole authority for its virtual variable targets."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self._targets: dict[tuple[str, str], TargetLease] = {}

    def authorize(self, gateway_id: str, variables: set[str], token: str) -> bool:
        """Call under lock, immediately before writing the target batch."""
        if not token or not variables:
            return False
        now = time.monotonic()
        return all(
            (lease := self._targets.get((gateway_id, variable))) is not None
            and lease.token == token
            and lease.expires_at > now
            for variable in variables
        )

    def claim(
        self,
        gateway_id: str,
        owner_id: str,
        variables: set[str],
        action: str,
        token: str,
        lease_ms: int,
    ) -> tuple[bool, str, str]:
        """Atomic all-or-none ACQUIRE, RENEW, RELEASE. Must hold self.lock."""
        if not gateway_id or not owner_id or not variables:
            return False, "", "INVALID_CLAIM"
        keys = [(gateway_id, variable) for variable in sorted(variables)]
        now = time.monotonic()
        for key in keys:
            lease = self._targets.get(key)
            if lease is not None and lease.expires_at <= now:
                self._targets.pop(key, None)
        if action == "ACQUIRE":
            if not 1000 <= lease_ms <= 60000 or token:
                return False, "", "INVALID_LEASE"
            if any(key in self._targets for key in keys):
                return False, "", "TARGET_BUSY"
            fencing_token = secrets.token_urlsafe(32)
            new_lease = TargetLease(owner_id, fencing_token, now + lease_ms / 1000)
            for key in keys:
                self._targets[key] = new_lease
            return True, fencing_token, ""
        if action not in {"RENEW", "RELEASE"} or not token:
            return False, "", "INVALID_LEASE"
        if any(
            (lease := self._targets.get(key)) is None
            or lease.token != token
            or lease.owner_id != owner_id
            for key in keys
        ):
            return False, "", "LEASE_LOST"
        if action == "RENEW":
            if not 1000 <= lease_ms <= 60000:
                return False, "", "INVALID_LEASE"
            renewal = TargetLease(owner_id, token, now + lease_ms / 1000)
            for key in keys:
                self._targets[key] = renewal
        else:
            for key in keys:
                self._targets.pop(key)
        return True, token, ""
