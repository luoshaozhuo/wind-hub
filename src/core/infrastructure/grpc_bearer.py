"""Internal gRPC bearer identities on an isolated network (no certificate fallback)."""

from __future__ import annotations

import hmac
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol


class BearerAuthenticationError(PermissionError):
    """Absent, malformed or unknown authenticated principal."""


class BearerAuthorizationError(PermissionError):
    """Authenticated principal lacks an explicitly granted operation."""


class GrpcIdentityContext(Protocol):
    def invocation_metadata(self) -> Sequence[tuple[str, str | bytes]]:
        """Expose transport metadata without trusting caller-declared identity."""


@dataclass(frozen=True, slots=True)
class CallerGrant:
    caller_id: str
    token_env: str
    permissions: frozenset[str]


def token_environment(reference: str) -> str:
    if not isinstance(reference, str) or re.fullmatch(
        r"env:[A-Z_][A-Z0-9_]*", reference
    ) is None:
        raise ValueError("bearer credential must reference env:NAME")
    return reference.removeprefix("env:")


def bearer_metadata(reference: str) -> tuple[tuple[str, str], ...]:
    secret = os.getenv(token_environment(reference), "")
    if len(secret.encode("utf-8")) < 32 or any(ch in secret for ch in "\r\n"):
        raise BearerAuthenticationError("bearer credential missing or invalid")
    return (("authorization", f"Bearer {secret}"),)


class BearerAuthorizer:
    """Fail-closed per-principal authorization with constant-time token matching."""

    def __init__(self, callers: Sequence[CallerGrant]) -> None:
        if not callers:
            raise ValueError("at least one authorized caller is required")
        self._callers = tuple(callers)
        ids: set[str] = set()
        sources: set[str] = set()
        values: set[str] = set()
        for caller in callers:
            if (
                not caller.caller_id or not caller.permissions
                or not isinstance(caller.token_env, str)
            ):
                raise ValueError("caller identity, token source and permissions required")
            variable = token_environment(caller.token_env)
            secret = os.getenv(variable, "")
            if len(secret.encode("utf-8")) < 32 or any(c in secret for c in "\r\n"):
                raise ValueError("bearer token must contain at least 32 safe bytes")
            if caller.caller_id in ids or variable in sources or secret in values:
                raise ValueError("bearer credentials must be unique for each caller")
            ids.add(caller.caller_id)
            sources.add(variable)
            values.add(secret)

    @staticmethod
    def from_mapping(
        credentials: Mapping[str, str], permissions: Mapping[str, Sequence[str]],
    ) -> BearerAuthorizer:
        if not credentials or credentials.keys() != permissions.keys():
            raise ValueError("token sources and permission principals disagree")
        return BearerAuthorizer(tuple(
            CallerGrant(caller_id, reference, frozenset(permissions[caller_id]))
            for caller_id, reference in sorted(credentials.items())
        ))

    def identify(self, context: GrpcIdentityContext) -> CallerGrant:
        """Return authenticated principal only after uniquely matching one secret."""
        # grpc.aio.Metadata iterates (key, value) pairs, not objects with
        # .key/.value attributes. Reject repeated or binary credentials.
        values = [
            value for key, value in context.invocation_metadata()
            if key.lower() == "authorization"
        ]
        if (
            len(values) != 1 or not isinstance(values[0], str)
            or not values[0].startswith("Bearer ")
        ):
            raise BearerAuthenticationError("missing or ambiguous bearer credentials")
        candidate = values[0][7:]
        matched: CallerGrant | None = None
        for caller in self._callers:
            secret = os.getenv(token_environment(caller.token_env), "")
            if secret and hmac.compare_digest(candidate, secret):
                matched = caller
        if matched is None:
            raise BearerAuthenticationError("invalid bearer credentials")
        return matched

    def require(self, context: GrpcIdentityContext, permission: str) -> str:
        principal = self.identify(context)
        if permission not in principal.permissions and "admin" not in principal.permissions:
            raise BearerAuthorizationError("caller is not authorized for operation")
        return principal.caller_id
