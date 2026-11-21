"""Immutable persistence values for the Configuration Repository."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ConfigIdentity:
    config_type: str
    config_id: str

    def __post_init__(self) -> None:
        if not self.config_type or not self.config_id:
            raise ValueError("config type and id must be nonempty")
        if self.config_type.strip() != self.config_type:
            raise ValueError("config type must be trimmed")
        if self.config_id.strip() != self.config_id:
            raise ValueError("config id must be trimmed")
        for identity in (self.config_type, self.config_id):
            if identity in (".", "..") or any(
                char in identity for char in ("/", "\\", "\x00", "\n", "\r")
            ):
                raise ValueError("configuration identities must not contain path separators")


@dataclass(frozen=True, slots=True)
class PreparedDocument:
    identity: ConfigIdentity
    schema_ref: str
    file_version: str
    content_sha256: str
    canonical_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RowProjection:
    """One normalized record; config_version_id is filled in at publication."""

    table: str
    columns: dict[str, object]


@dataclass(frozen=True, slots=True)
class PreparedVersion:
    document: PreparedDocument
    version_id: str
    projections: tuple[RowProjection, ...]
    references: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class CommitResult:
    revision_id: int
    version_ids: tuple[str, ...]
    replayed: bool = False


class RevisionConflictError(Exception):
    """Expected revision differs from the committed repository revision."""


class IdempotencyConflictError(Exception):
    """One actor reused its request identifier with different input."""


class RepositoryConflictError(Exception):
    """An immutable identity or version was reused incompatibly."""


class RepositoryCapacityExceededError(Exception):
    """A configured candidate, document, backup, or response quota was exceeded."""


class RepositoryVersionExistsError(Exception):
    """The immutable file version already exists for this document."""


class RepositoryNotFoundError(Exception):
    """The requested repository object does not exist."""
