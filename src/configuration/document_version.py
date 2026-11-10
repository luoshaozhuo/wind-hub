"""Version metadata for shared instance and template catalog documents."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.application.errors import ConfigError
from core.infrastructure.config import ConfigOperationError, parse_yaml

_FILE_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")


@dataclass(frozen=True, slots=True)
class DocumentVersion:
    schema_version: int
    file_version: str


def validate_document_version(
    document: Mapping[str, Any],
    path: str | Path,
) -> DocumentVersion:
    """Reject unsupported schemas and missing or malformed file versions."""
    schema_version = document.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise ConfigError(f"{path}: unsupported schema_version {schema_version!r}")
    file_version = document.get("file_version")
    if not isinstance(file_version, str) or not _FILE_VERSION.fullmatch(file_version):
        raise ConfigError(f"{path}: invalid file_version {file_version!r}")
    return DocumentVersion(schema_version=schema_version, file_version=file_version)


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    """Auditable raw-byte identity of an actually loaded shared document."""

    path: Path
    sha256: str
    schema_version: int
    file_version: str
    logical_key: str = ""


_READ_IDENTITIES: ContextVar[dict[Path, SourceIdentity] | None] = ContextVar(
    "shared_config_source_hashes", default=None
)


@contextmanager
def verify_shared_source_closure() -> Iterator[dict[Path, SourceIdentity]]:
    """Check raw-byte identities of every source again after full resolution.

    Caller holds the cooperative shared lock. This second pass detects
    non-cooperative multi-file replacements between individual file reads.
    """
    identities: dict[Path, SourceIdentity] = {}
    token = _READ_IDENTITIES.set(identities)
    try:
        yield identities
        for path, identity in identities.items():
            try:
                final_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                raise ConfigError(f"{path}: SOURCE_CHANGED during closure verification") from exc
            if identity.sha256 != final_hash:
                raise ConfigError(
                    f"{path}: SOURCE_CHANGED sha256={identity.sha256} -> {final_hash}"
                )
    finally:
        _READ_IDENTITIES.reset(token)


def read_stable_document(path: Path) -> dict[str, Any]:
    """Read one versioned document and reject changes during parsing.

    A cooperative shared configuration lock must still cover the entire
    multi-file reference closure; this per-file guard is not a transaction.
    """
    try:
        original = path.read_bytes()
        document = parse_yaml(original.decode("utf-8"))
        current = path.read_bytes()
    except FileNotFoundError as exc:
        raise ConfigError(f"{path}: REFERENCE_NOT_FOUND") from exc
    except PermissionError as exc:
        raise ConfigError(f"{path}: PERMISSION_DENIED") from exc
    except UnicodeError as exc:
        raise ConfigError(f"{path}: SCHEMA_ERROR invalid UTF-8") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: configuration source I/O failed") from exc
    except ConfigOperationError as exc:
        raise ConfigError(f"{path}: SCHEMA_ERROR ({exc.code.value})") from exc
    if original != current:
        before = hashlib.sha256(original).hexdigest()
        after = hashlib.sha256(current).hexdigest()
        raise ConfigError(f"{path}: SOURCE_CHANGED sha256={before} -> {after}")
    version = validate_document_version(document, path)
    identities = _READ_IDENTITIES.get()
    if identities is not None:
        canonical_path = path.absolute()
        digest = hashlib.sha256(original).hexdigest()
        prior = identities.setdefault(
            canonical_path,
            SourceIdentity(
                path=canonical_path,
                sha256=digest,
                schema_version=version.schema_version,
                file_version=version.file_version,
            ),
        )
        if prior.sha256 != digest:
            raise ConfigError(f"{canonical_path}: SOURCE_CHANGED sha256={prior.sha256} -> {digest}")
    return document
