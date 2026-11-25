"""Typed, immutable configuration identity and validation for Sink Service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any


class ProcessRole(StrEnum):
    REALTIME = "realtime"
    RELIABLE = "reliable"


class DesiredState(StrEnum):
    START = "START"
    STOP = "STOP"


class ConfigurationError(ValueError):
    """A supplied snapshot violates the frozen configuration contract."""


class InvalidConfigurationError(ConfigurationError):
    """The request payload itself is invalid; maps to gRPC INVALID_ARGUMENT."""


class RevisionConflictError(ConfigurationError):
    """expected_revision no longer matches the authoritative revision (ABORTED)."""


class WriterRequiredError(ConfigurationError):
    """A mutating call reached a non-writer context; maps to PERMISSION_DENIED."""


class CandidateNotFoundError(ConfigurationError):
    """The candidate id is unknown or expired; maps to gRPC NOT_FOUND."""


@dataclass(frozen=True, slots=True)
class SnapshotFile:
    path: str
    content: bytes

    def __post_init__(self) -> None:
        path = PurePosixPath(self.path)
        if (
            not self.path
            or self.path.startswith("/")
            or "\\" in self.path
            or any(part in ("", ".", "..") for part in self.path.split("/"))
            or path.as_posix() != self.path
        ):
            raise ConfigurationError(f"unsafe relative configuration path: {self.path!r}")
        if not (self.path.startswith("configs/") and self.path.endswith(".yaml")):
            raise ConfigurationError(f"unsupported configuration path: {self.path!r}")


@dataclass(frozen=True, slots=True)
class ValidatedSnapshot:
    revision: str
    files: tuple[SnapshotFile, ...]
    service: dict[str, Any]
    sinks: dict[str, Any]
    deliveries: dict[str, Any]
    documents: dict[str, dict[str, Any]]


def expect_mapping(value: object, location: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ConfigurationError(f"{location}: expected mapping with string keys")
    return value


def require_keys(
    mapping: dict[str, Any],
    location: str,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
) -> None:
    """Reject unknown and missing configuration fields before using values."""
    actual = set(mapping)
    missing = required - actual
    unknown = actual - required - optional
    if missing or unknown:
        raise ConfigurationError(
            f"{location}: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def expect_positive_int(value: object, location: str, maximum: int | None = None) -> int:
    if type(value) is not int or value <= 0 or (maximum is not None and value > maximum):
        raise ConfigurationError(f"{location}: invalid positive integer")
    return value


def expect_nonempty_str(value: object, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{location}: expected non-empty string")
    return value


def require_version(doc: dict[str, Any], location: str, *, schema_version: int = 1) -> None:
    if type(doc.get("schema_version")) is not int or doc["schema_version"] != schema_version:
        raise ConfigurationError(f"{location}: unsupported schema_version")
    version = expect_nonempty_str(doc.get("file_version"), f"{location}.file_version")
    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ConfigurationError(f"{location}: invalid file_version")
