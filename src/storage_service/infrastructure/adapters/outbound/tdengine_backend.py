"""TDengine 3.3.6 WebSocket adapter with typed STMT2 parameter binding.

All external connector calls are confined to this outbound adapter. It must pass
the target-version DDL/driver Spike before the service is marked operational.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from collections import defaultdict
from importlib import import_module
from typing import Any, Protocol, Sequence, cast

from storage_service.application.config import StorageConfig
from storage_service.domain.naming import StorageObjectNames, get_object_names
from storage_service.domain.sample import DataLevel, StorageSample, ValueKind
from storage_service.infrastructure.adapters.outbound.metadata_store import (
    MetadataStore,
)
from storage_service.infrastructure.adapters.outbound.tdengine_schema import (
    SCHEMA_FINGERPRINT,
    DatabaseRetention,
    create_database_sql,
    create_stable_sql,
    create_table_sql,
    expected_columns,
    expected_tags,
)


class _PreparedStatement(Protocol):
    def prepare(self, sql: str) -> None: ...
    def bind(self, params: list[object]) -> None: ...
    def execute(self) -> int: ...
    def close(self) -> None: ...


class _TaosConnection(Protocol):
    def query(self, sql: str) -> Any: ...
    def execute(self, sql: str) -> int: ...
    def stmt2_statement(self) -> _PreparedStatement: ...
    def close(self) -> None: ...


class _TaosModule(Protocol):
    def connect(
        self, *, user: str, password: str, host: str, port: int
    ) -> _TaosConnection: ...

    def stmt2_bind_param_view(
        self, *, table_name: str | None, tags: list[object] | None, columns: list[object]
    ) -> object: ...

    def micros_timestamps_to_column(self, values: list[int | None]) -> object: ...
    def varchar_to_column(self, values: list[str | None]) -> object: ...
    def tiny_ints_to_column(self, values: list[int | None]) -> object: ...
    def big_ints_to_column(self, values: list[int | None]) -> object: ...
    def unsigned_big_ints_to_column(self, values: list[int | None]) -> object: ...
    def doubles_to_column(self, values: list[float | None]) -> object: ...
    def bools_to_column(self, values: list[bool | None]) -> object: ...
    def varbinary_to_column(self, values: list[bytes | None]) -> object: ...


_HOST_PORT = re.compile(r"(?P<host>[a-zA-Z0-9.-]+):(?P<port>[0-9]{1,5})\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_QUALITY = {"good": 1, "bad": 2, "uncertain": 3}
_LEVEL = {DataLevel.RAW: 1, DataLevel.PROTOCOL: 2, DataLevel.BUSINESS: 3}


class TDengineSchemaConflict(RuntimeError):
    """Physical schema/tag/ownership does not match the frozen Storage contract."""


def _columns(samples: Sequence[StorageSample], module: _TaosModule) -> list[object]:
    """Bind all 18 columns in the exact frozen TDengine column order."""
    for sample in samples:
        if not _HEX.fullmatch(sample.sample_key):
            raise ValueError("invalid sample key")
    return [
        module.micros_timestamps_to_column([s.sample_time_us for s in samples]),
        module.varchar_to_column([s.sample_key for s in samples]),
        module.varchar_to_column([s.logical_key for s in samples]),
        module.varchar_to_column([s.content_sha256 for s in samples]),
        module.varchar_to_column([s.sample_id for s in samples]),
        module.varchar_to_column([s.point_id for s in samples]),
        module.varchar_to_column([s.business_point_id for s in samples]),
        module.tiny_ints_to_column([_LEVEL[s.data_level] for s in samples]),
        module.tiny_ints_to_column([int(s.value_kind) for s in samples]),
        module.big_ints_to_column([
            int(s.value) if s.value_kind == ValueKind.INT64 else None for s in samples
        ]),
        module.unsigned_big_ints_to_column([
            int(s.value) if s.value_kind == ValueKind.UINT64 else None for s in samples
        ]),
        module.doubles_to_column([
            float(s.value) if s.value_kind == ValueKind.DOUBLE else None for s in samples
        ]),
        module.bools_to_column([
            bool(s.value) if s.value_kind == ValueKind.BOOL else None for s in samples
        ]),
        module.varchar_to_column([
            str(s.value) if s.value_kind == ValueKind.TEXT else None for s in samples
        ]),
        module.varbinary_to_column([
            bytes(s.value) if s.value_kind == ValueKind.BYTES else None for s in samples
        ]),
        module.tiny_ints_to_column([_QUALITY[s.quality.value] for s in samples]),
        module.micros_timestamps_to_column([s.publish_time_us for s in samples]),
        module.micros_timestamps_to_column([s.receive_time_us for s in samples]),
    ]


class TDengineBackend:
    """Single-instance initial adapter; all calls serialized on one connection."""

    def __init__(self, *, config: StorageConfig, metadata: MetadataStore) -> None:
        self._config = config
        self._metadata = metadata
        self._driver: _TaosModule | None = None
        self._connection: _TaosConnection | None = None
        self._lock = threading.RLock()
        self._verified_tables: set[str] = set()

    async def open(self) -> None:
        await asyncio.to_thread(self._open_sync)

    def _open_sync(self) -> None:
        match = _HOST_PORT.fullmatch(self._config.tdengine.endpoint)
        if match is None:
            raise ValueError("TDengine endpoint must be hostname:port")
        host = match.group("host")
        port = int(match.group("port"))
        if not 1 <= port <= 65535:
            raise ValueError("invalid TDengine port")
        ref = self._config.tdengine.credential_ref.removeprefix("env:")
        user = os.environ.get("WIND_HUB_TDENGINE_USER")
        password = os.environ.get(ref)
        if not user or not password:
            raise ValueError("TDengine username or credential environment is missing")
        module = cast(_TaosModule, import_module("taosws"))
        with self._lock:
            if self._connection is not None:
                raise RuntimeError("TDengine adapter is already open")
            self._connection = module.connect(
                user=user, password=password, host=host, port=port
            )
            self._driver = module

    async def close(self) -> None:
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
            self._connection = None
            self._driver = None
            self._verified_tables.clear()

    def _resources(self) -> tuple[_TaosConnection, _TaosModule]:
        if self._connection is None or self._driver is None:
            raise RuntimeError("TDengine connection is not open")
        return self._connection, self._driver

    async def ensure_tables(self, samples: Sequence[StorageSample]) -> None:
        await asyncio.to_thread(self._ensure_tables_sync, samples)

    @staticmethod
    def _identity_json(values: tuple[str, ...]) -> str:
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))

    def _ensure_tables_sync(self, samples: Sequence[StorageSample]) -> None:
        with self._lock:
            connection, _ = self._resources()
            retention = DatabaseRetention.parse(
                duration=self._config.tdengine.database.duration,
                keep=self._config.tdengine.database.keep,
            )
            seen: set[str] = set()
            for sample in samples:
                names = get_object_names(sample, SCHEMA_FINGERPRINT)
                if names.table in seen or names.table in self._verified_tables:
                    continue
                seen.add(names.table)
                identities = (
                    (names.database, names.database_identity_sha256,
                     (sample.wind_farm_id,)),
                    (names.stable, names.stable_identity_sha256,
                     (sample.wind_farm_id, sample.device_type,
                      sample.point_table_id, sample.data_level.value,
                      SCHEMA_FINGERPRINT)),
                    (names.table, names.table_identity_sha256,
                     (sample.wind_farm_id, sample.device_type, sample.device_id,
                      sample.point_table_id, sample.data_level.value,
                      SCHEMA_FINGERPRINT)),
                )
                registrations = [
                    self._metadata.reserve_object(
                        name, digest, self._identity_json(identity), SCHEMA_FINGERPRINT
                    )
                    for name, digest, identity in identities
                ]
                connection.execute(create_database_sql(names, retention))
                connection.execute(create_stable_sql(names))
                connection.execute(
                    create_table_sql(
                        names,
                        identity_hash=names.table_identity_sha256,
                        point_table_hash=hashlib.sha256(
                            sample.point_table_id.encode("utf-8")
                        ).hexdigest(),
                        device_hash=hashlib.sha256(
                            sample.device_id.encode("utf-8")
                        ).hexdigest(),
                        level_tag=_LEVEL[sample.data_level],
                    )
                )
                # CREATE IF NOT EXISTS is not verification. A mismatched existing
                # object is a hard stop; never mark CREATING as READY by guessing.
                self._verify_table(
                    connection, names, sample,
                    expected_point_hash=hashlib.sha256(
                        sample.point_table_id.encode("utf-8")
                    ).hexdigest(),
                    expected_device_hash=hashlib.sha256(
                        sample.device_id.encode("utf-8")
                    ).hexdigest(),
                )
                for registration in registrations:
                    self._metadata.mark_ready(registration, verified=True)
                # Single managed process: once readback verified, future batches
                # must not emit DDL or repeat expensive schema/ownership checks.
                self._verified_tables.add(names.table)

    @staticmethod
    def _verify_table(
        connection: _TaosConnection,
        names: StorageObjectNames,
        sample: StorageSample,
        *,
        expected_point_hash: str,
        expected_device_hash: str,
    ) -> None:
        columns = list(connection.query(f"DESCRIBE {names.database}.{names.table}"))
        wanted = expected_columns()
        if len(columns) < len(wanted):
            raise TDengineSchemaConflict("missing TDengine data columns")
        for observed, (wanted_name, wanted_type) in zip(columns, wanted, strict=False):
            if str(observed[0]).lower() != wanted_name:
                raise TDengineSchemaConflict(f"mismatched TDengine column: {wanted_name}")
            # Connector DESCRIBE supplies type name and length separately; the full
            # tuple's precision/type compatibility is checked by the G-01 Spike.
            expected_base = wanted_type.split("(")[0].split(" ")[0]
            if str(observed[1]).upper() != expected_base:
                raise TDengineSchemaConflict(f"mismatched TDengine type: {wanted_name}")

        tag_rows = list(connection.query(f"SHOW TAGS FROM {names.database}.{names.table}"))
        tag_map = {str(row[3]): str(row[5]) for row in tag_rows}
        expected = {
            "identity_hash": names.table_identity_sha256,
            "point_table_hash": expected_point_hash,
            "device_hash": expected_device_hash,
            "level_tag": str(_LEVEL[sample.data_level]),
        }
        if any(tag_map.get(name) != value for name, value in expected.items()):
            raise TDengineSchemaConflict("TDengine table TAG identity mismatch")
        if len(tag_map) != len(expected_tags()):
            raise TDengineSchemaConflict("TDengine TAG count mismatch")

    async def fetch_content_hashes(
        self, samples: Sequence[StorageSample]
    ) -> dict[str, frozenset[str]]:
        return await asyncio.to_thread(self._fetch_content_hashes_sync, samples)

    def _fetch_content_hashes_sync(
        self, samples: Sequence[StorageSample]
    ) -> dict[str, frozenset[str]]:
        """Batch readback using bounded time ranges and generated SHA-256 keys."""
        grouped: dict[str, list[StorageSample]] = defaultdict(list)
        result: dict[str, set[str]] = {}
        for sample in samples:
            names = get_object_names(sample, SCHEMA_FINGERPRINT)
            grouped[f"{names.database}.{names.table}"].append(sample)
            result.setdefault(sample.logical_key, set())
        with self._lock:
            connection, _ = self._resources()
            for table, entries in grouped.items():
                # TDengine can push the time predicate to partition pruning.
                # Keep each IN list short and distinct to bound statement size.
                unique = list({entry.logical_key: entry for entry in entries}.values())
                for begin in range(0, len(unique), 128):
                    chunk = unique[begin: begin + 128]
                    first_us = min(entry.sample_time_us for entry in chunk)
                    last_us = max(entry.sample_time_us for entry in chunk)
                    keys = ", ".join(f"'{entry.logical_key}'" for entry in chunk)
                    rows = connection.query(
                        f"SELECT logical_key, content_sha256 FROM {table} "
                        f"WHERE ts >= {first_us} AND ts <= {last_us} "
                        f"AND logical_key IN ({keys})"
                    )
                    for row in rows:
                        logical_key = str(row[0])
                        value_hash = str(row[1])
                        if logical_key not in result:
                            raise TDengineSchemaConflict("unexpected logical key from database")
                        if not _HEX.fullmatch(value_hash):
                            raise TDengineSchemaConflict("invalid content hash in database")
                        result[logical_key].add(value_hash)
        return {key: frozenset(values) for key, values in result.items()}

    async def insert_batch(self, samples: Sequence[StorageSample]) -> None:
        await asyncio.to_thread(self._insert_batch_sync, samples)

    def _insert_batch_sync(self, samples: Sequence[StorageSample]) -> None:
        if not samples:
            return
        groups: dict[str, list[StorageSample]] = defaultdict(list)
        for sample in samples:
            names = get_object_names(sample, SCHEMA_FINGERPRINT)
            groups[f"{names.database}.{names.table}"].append(sample)
        with self._lock:
            connection, driver = self._resources()
            for table_name, batch in groups.items():
                if len(batch) > self._config.tdengine.batch_max_records:
                    raise ValueError("TDengine batch record count exceeds configured maximum")
                statement = connection.stmt2_statement()
                try:
                    placeholders = ", ".join("?" for _ in expected_columns())
                    statement.prepare(f"INSERT INTO ? VALUES ({placeholders})")
                    binding = driver.stmt2_bind_param_view(
                        table_name=table_name, tags=None, columns=_columns(batch, driver)
                    )
                    statement.bind([binding])
                    affected = statement.execute()
                    # TDengine duplicate-key replay may report a different row count.
                    # Never infer success from affected rows: caller verifies every key.
                    if affected < 0:
                        raise RuntimeError("TDengine reported a negative affected count")
                finally:
                    statement.close()
