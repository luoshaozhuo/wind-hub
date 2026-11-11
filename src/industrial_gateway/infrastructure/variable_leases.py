"""Gateway-scoped target leases protecting concurrent variable injections."""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass, replace
from time import monotonic

from industrial_gateway.application.variable_store import (
    Scalar,
    UpdateResult,
    VariableStore,
)


class LeaseError(RuntimeError):
    """A target claim is absent, expired, or owned by another writer."""


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    gateway_id: str
    owner_id: str
    variable_ids: tuple[str, ...]
    action: str
    lease_token: str = ""
    lease_ms: int = 0


@dataclass(frozen=True, slots=True)
class ClaimResult:
    accepted: bool
    lease_token: str = ""
    error_code: str = ""
    message: str = ""


@dataclass(frozen=True, slots=True)
class TargetLease:
    owner_id: str
    token: str
    names: frozenset[str]
    expires_at: float


class VariableLeases:
    """Atomic claim and variable-update guards, scoped to one Gateway ID."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._claims: dict[str, list[TargetLease]] = {}

    def _lock(self, gateway_id: str) -> asyncio.Lock:
        return self._locks.setdefault(gateway_id, asyncio.Lock())

    def _active(self, gateway_id: str) -> list[TargetLease]:
        now = monotonic()
        active = [lease for lease in self._claims.get(gateway_id, []) if lease.expires_at > now]
        self._claims[gateway_id] = active
        return active

    async def claim(
        self,
        request: ClaimRequest,
        available: frozenset[str],
    ) -> ClaimResult:
        """ACQUIRE all-or-nothing; RENEW/RELEASE require the exact old claim."""
        gateway_id = request.gateway_id
        async with self._lock(gateway_id):
            claims = self._active(gateway_id)
            names = frozenset(request.variable_ids)
            if (
                not request.owner_id.strip()
                or not names
                or len(names) != len(request.variable_ids)
                or len(names) > 5000
            ):
                return ClaimResult(accepted=False, error_code="INVALID_CLAIM")
            if request.action == "ACQUIRE":
                if not names <= available:
                    return ClaimResult(accepted=False, error_code="INVALID_TARGET")
                if not 1000 <= request.lease_ms <= 60000:
                    return ClaimResult(accepted=False, error_code="INVALID_LEASE_MS")
                if any(names & claim.names for claim in claims):
                    return ClaimResult(accepted=False, error_code="TARGET_CLAIMED")
                token = secrets.token_urlsafe(32)
                lease = TargetLease(
                    owner_id=request.owner_id,
                    token=token,
                    names=names,
                    expires_at=monotonic() + request.lease_ms / 1000,
                )
                claims.append(lease)
                return ClaimResult(accepted=True, lease_token=token)
            existing = next(
                (claim for claim in claims if claim.token == request.lease_token),
                None,
            )
            if existing is None or existing.owner_id != request.owner_id or existing.names != names:
                return ClaimResult(accepted=False, error_code="LEASE_LOST")
            if request.action == "RELEASE":
                claims.remove(existing)
                return ClaimResult(accepted=True)
            if request.action == "RENEW":
                if not 1000 <= request.lease_ms <= 60000:
                    return ClaimResult(accepted=False, error_code="INVALID_LEASE_MS")
                claims[claims.index(existing)] = replace(
                    existing,
                    expires_at=monotonic() + request.lease_ms / 1000,
                )
                return ClaimResult(accepted=True, lease_token=existing.token)
            return ClaimResult(accepted=False, error_code="INVALID_ACTION")

    async def update(
        self,
        gateway_id: str,
        store: VariableStore,
        values: list[tuple[str, Scalar | None]],
        lease_token: str,
    ) -> list[UpdateResult]:
        """Fence the complete batch before any per-item data publication."""
        async with self._lock(gateway_id):
            active = self._active(gateway_id)
            requested = {variable_id for variable_id, _ in values}
            if len(requested) != len(values):
                raise ValueError("duplicate variable_id in UpdateVariables")
            owned = next(
                (claim for claim in active if claim.token == lease_token),
                None,
            )
            if owned is None or not requested <= owned.names:
                raise LeaseError("LEASE_LOST")
            results: list[UpdateResult] = []
            for variable_id, value in values:
                if value is None:
                    results.append(
                        UpdateResult(
                            variable_id,
                            False,
                            "INVALID_VALUE",
                            "missing scalar value",
                        )
                    )
                    continue
                try:
                    await store.update(variable_id, value)
                except KeyError as exc:
                    results.append(UpdateResult(variable_id, False, "NOT_FOUND", str(exc)))
                except (TypeError, ValueError) as exc:
                    results.append(UpdateResult(variable_id, False, "INVALID_VALUE", str(exc)))
                else:
                    results.append(UpdateResult(variable_id, True))
            return results

    def invalidate(self, gateway_id: str) -> None:
        """Fence old writer tokens when a Server is removed or reconfigured."""
        self._claims.pop(gateway_id, None)
