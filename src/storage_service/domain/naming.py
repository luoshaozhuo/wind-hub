"""TDengine names are deterministic and never include raw untrusted identifiers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from storage_service.domain.sample import StorageSample


def _identity_digest(values: tuple[str, ...]) -> str:
    if not values or any(not value or not isinstance(value, str) for value in values):
        raise ValueError("physical object identity must contain nonempty strings")
    encoded = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class StorageObjectNames:
    database: str
    stable: str
    table: str
    database_identity_sha256: str
    stable_identity_sha256: str
    table_identity_sha256: str


def get_object_names(sample: StorageSample, schema_fingerprint: str) -> StorageObjectNames:
    """Map original identities to fixed 96-bit names; persist full hashes separately."""
    if len(schema_fingerprint) != 64 or any(
        ch not in "0123456789abcdef" for ch in schema_fingerprint
    ):
        raise ValueError("schema_fingerprint must be lowercase SHA-256")
    db_id = (sample.wind_farm_id,)
    stable_id = (
        sample.wind_farm_id, sample.device_type, sample.point_table_id,
        sample.data_level.value, schema_fingerprint,
    )
    table_id = (
        sample.wind_farm_id, sample.device_type, sample.device_id,
        sample.point_table_id, sample.data_level.value, schema_fingerprint,
    )
    db = _identity_digest(db_id)
    stable = _identity_digest(stable_id)
    table = _identity_digest(table_id)
    return StorageObjectNames(
        database=f"wh_{db[:24]}",
        stable=f"st_{stable[:24]}",
        table=f"tb_{table[:24]}",
        database_identity_sha256=db,
        stable_identity_sha256=stable,
        table_identity_sha256=table,
    )
