"""Internal TDengine object identifiers for immutable wide-table storage."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StorageObjectNames:
    database: str
    stable: str
    table: str
    database_identity_sha256: str
    stable_identity_sha256: str
    table_identity_sha256: str
