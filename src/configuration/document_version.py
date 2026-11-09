"""Version metadata for shared instance and template catalog documents."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
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

def read_stable_document(path: Path) -> tuple[dict[str, Any], str]:
    """Read one versioned document and reject changes during parsing.

    A cooperative shared configuration lock must still cover the entire
    multi-file reference closure; this per-file guard is not a transaction.
    """
    try:
        original = path.read_bytes()
        document = parse_yaml(original.decode("utf-8"))
        current = path.read_bytes()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"{path}: configuration source I/O failed") from exc
    except ConfigOperationError as exc:
        raise ConfigError(f"{path}: configuration parse failed ({exc.code.value})") from exc
    if original != current:
        before = hashlib.sha256(original).hexdigest()
        after = hashlib.sha256(current).hexdigest()
        raise ConfigError(f"{path}: SOURCE_CHANGED sha256={before} -> {after}")
    validate_document_version(document, path)
    return document, hashlib.sha256(original).hexdigest()
