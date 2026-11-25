"""TDengine 3.3.6 WebSocket adapter with typed STMT2 parameter binding.

All external connector calls are confined to this outbound adapter. It must pass
the target-version DDL/driver Spike before the service is marked operational.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import queue
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from importlib import import_module
from typing import Any, Protocol, TypeVar, cast

from storage_service.application.config import StorageConfig
from storage_service.application.query import HistoryFilter, HistoryRow
from storage_service.domain.naming import StorageObjectNames, get_object_names
from storage_service.domain.sample import DataLevel, StorageSample, ValueKind
from storage_service.domain.wide_record import WideRecord
from storage_service.domain.wide_schema import WideSchema
from storage_service.infrastructure.adapters.outbound.metadata_store import (
    MetadataStore,
)
from storage_service.infrastructure.adapters.outbound.wide_ddl import stable_ddl, subtable_ddl
from storage_service.infrastructure.adapters.outbound.wide_stmt2 import write_wide_stmt2
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
        self, *, user: str, password: str, host: str, port: int, read_timeout: str
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
_LOG = logging.getLogger(__name__)
_QUALITY = {"good": 1, "bad": 2, "uncertain": 3}
_LEVEL = {DataLevel.RAW: 1, DataLevel.PROTOCOL: 2, DataLevel.BUSINESS: 3}
_Result = TypeVar("_Result")


class _OperationLease:
    """标记调用方已离开；工作线程结束后必须销毁而非复用连接。"""

    def __init__(self, timeout_ms: int) -> None:
        self.timeout_s = timeout_ms / 1000
        self._deadline = time.monotonic() + self.timeout_s
        self._expired = threading.Event()

    @property
    def expired(self) -> bool:
        return self._expired.is_set()

    def expire(self) -> None:
        self._expired.set()

    def remaining(self) -> float:
        remaining = self._deadline - time.monotonic()
        if self.expired or remaining <= 0:
            self.expire()
            raise TimeoutError("TDengine operation deadline exceeded")
        return remaining


class TDengineSchemaConflictError(RuntimeError):
    """Physical schema/tag/ownership does not match the frozen Storage contract."""


def _stored_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TDengineSchemaConflictError("stored integer column lost its type")
    return value


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
        module.big_ints_to_column(
            [int(cast(int, s.value)) if s.value_kind == ValueKind.INT64 else None for s in samples]
        ),
        module.unsigned_big_ints_to_column(
            [int(cast(int, s.value)) if s.value_kind == ValueKind.UINT64 else None for s in samples]
        ),
        module.doubles_to_column(
            [
                float(cast(int | float, s.value)) if s.value_kind == ValueKind.DOUBLE else None
                for s in samples
            ]
        ),
        module.bools_to_column(
            [bool(s.value) if s.value_kind == ValueKind.BOOL else None for s in samples]
        ),
        module.varchar_to_column(
            [str(s.value) if s.value_kind == ValueKind.TEXT else None for s in samples]
        ),
        module.varbinary_to_column(
            [
                bytes(cast(bytes, s.value)) if s.value_kind == ValueKind.BYTES else None
                for s in samples
            ]
        ),
        module.tiny_ints_to_column([_QUALITY[s.quality.value] for s in samples]),
        module.micros_timestamps_to_column([s.publish_time_us for s in samples]),
        module.micros_timestamps_to_column([s.receive_time_us for s in samples]),
    ]


class TDengineBackend:
    """有界 taosws 连接池；超时工作不得复用连接或无限增加线程。"""

    def __init__(self, *, config: StorageConfig, metadata: MetadataStore) -> None:
        self._config = config
        self._metadata = metadata
        self._driver: _TaosModule | None = None
        self._pool: queue.Queue[_TaosConnection] = queue.Queue(
            maxsize=config.tdengine.max_inflight_sql
        )
        self._pool_size = 0
        self._lock = threading.RLock()
        self._ddl_lock = threading.RLock()
        self._verified_tables: set[str] = set()
        self._opening = False
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=config.tdengine.max_inflight_sql,
            thread_name_prefix="storage-tdengine",
        )
        self._close_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="storage-tdengine-close"
        )

    @staticmethod
    async def _await_bounded(future: Future[_Result], lease: _OperationLease) -> _Result:
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout=lease.timeout_s)
        except TimeoutError as exc:
            lease.expire()
            future.cancel()
            raise TimeoutError("TDengine operation deadline exceeded") from exc
        except asyncio.CancelledError:
            lease.expire()
            future.cancel()
            raise

    async def _run_bounded(
        self, operation: Callable[..., _Result], *args: Any, **kwargs: Any
    ) -> _Result:
        """只等待到有界期限；线程超时不是 SQL 取消，由租约隔离其连接。"""
        lease = _OperationLease(self._config.tdengine.ingestion.write_timeout_ms)
        with self._lock:
            if self._closed:
                raise RuntimeError("TDengine adapter is closed")
            future = self._executor.submit(operation, *args, lease=lease, **kwargs)
        return await self._await_bounded(future, lease)

    async def open(self) -> None:
        await self._run_bounded(self._open_sync)

    def _connect(self, module: _TaosModule) -> _TaosConnection:
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
        return module.connect(
            user=user,
            password=password,
            host=host,
            port=port,
            # taosws 将此 DSN 参数传给 WebSocket 请求/响应及结果拉取。
            read_timeout=str(max(1, (self._config.tdengine.ingestion.write_timeout_ms + 999) // 1000)),
        )

    def _open_sync(self, *, lease: _OperationLease) -> None:
        module = cast(_TaosModule, import_module("taosws"))
        with self._lock:
            if self._closed:
                raise RuntimeError("TDengine adapter is closed")
            if self._opening or self._pool_size or self._driver is not None:
                raise RuntimeError("TDengine adapter is already open")
            self._opening = True
        opened: list[_TaosConnection] = []
        try:
            for _ in range(self._config.tdengine.max_inflight_sql):
                lease.remaining()
                opened.append(self._connect(module))
            with self._lock:
                if self._closed or lease.expired:
                    raise TimeoutError("TDengine open completed after its deadline")
                self._driver = module
                for conn in opened:
                    self._pool.put_nowait(conn)
                self._pool_size = len(opened)
        except BaseException:
            for conn in opened:
                conn.close()
            raise
        finally:
            with self._lock:
                self._opening = False

    async def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            # 关闭后禁止新工作及连接回池；重复关闭不再次提交执行器。
            self._closed = True
            self._driver = None
            self._verified_tables.clear()
            lease = _OperationLease(self._config.tdengine.ingestion.write_timeout_ms)
            # SQL 工作全部阻塞时，关闭仍须立即排空已空闲连接。
            future = self._close_executor.submit(self._close_sync, lease=lease)
        try:
            await self._await_bounded(future, lease)
        finally:
            with self._lock:
                # 无法强行杀死驱动线程；read_timeout 使其最终退出，容量始终有界。
                self._executor.shutdown(wait=False, cancel_futures=True)
                self._close_executor.shutdown(wait=False, cancel_futures=True)

    def _decrement_pool_size(self) -> None:
        with self._lock:
            self._pool_size -= 1

    def _close_sync(self, *, lease: _OperationLease) -> None:
        with self._lock:
            if self._closed and self._pool_size == 0:
                return
            self._closed = True
            pool_size = self._pool_size
            self._driver = None
            self._verified_tables.clear()
        first_error: Exception | None = None
        for _ in range(pool_size):
            try:
                conn = self._pool.get(timeout=lease.remaining())
            except (queue.Empty, TimeoutError) as exc:
                raise TimeoutError(
                    "cannot close TDengine: outstanding database operations"
                ) from exc
            try:
                conn.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
            finally:
                self._decrement_pool_size()
        if first_error is not None:
            raise first_error

    def _acquire_connection(self, lease: _OperationLease) -> tuple[_TaosConnection, _TaosModule]:
        lease.remaining()
        with self._lock:
            driver = self._driver
            if self._closed or driver is None:
                raise RuntimeError("TDengine adapter is not open")
        try:
            connection = self._pool.get_nowait()
            return connection, driver
        except queue.Empty:
            pass

        with self._lock:
            if self._closed or self._driver is not driver:
                raise RuntimeError("TDengine adapter is not open")
            if self._pool_size < self._config.tdengine.max_inflight_sql:
                self._pool_size += 1
                create = True
            else:
                create = False
        if create:
            try:
                connection = self._connect(driver)
                if lease.expired or self._closed or self._driver is not driver:
                    raise TimeoutError("TDengine connection finished after its deadline")
                return connection, driver
            except BaseException:
                if "connection" in locals():
                    connection.close()
                self._decrement_pool_size()
                raise

        try:
            return self._pool.get(timeout=lease.remaining()), driver
        except queue.Empty as exc:
            raise TimeoutError("TDengine connection pool exhausted") from exc

    def _discard_connection(self, connection: _TaosConnection) -> None:
        try:
            connection.close()
        except Exception:
            _LOG.warning("failed to close an unusable TDengine connection", exc_info=True)
        finally:
            self._decrement_pool_size()

    @contextmanager
    def _borrow_connection(
        self, *, lease: _OperationLease
    ) -> Iterator[tuple[_TaosConnection, _TaosModule]]:
        connection, driver = self._acquire_connection(lease)
        reusable = False
        try:
            yield connection, driver
            reusable = not lease.expired
        finally:
            with self._lock:
                can_return = reusable and not self._closed and self._driver is driver
            if can_return:
                self._pool.put_nowait(connection)
            else:
                self._discard_connection(connection)

    async def ensure_tables(self, samples: Sequence[StorageSample]) -> None:
        await self._run_bounded(self._ensure_tables_sync, samples=samples)

    @staticmethod
    def _identity_json(values: tuple[str, ...]) -> str:
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))

    def _ensure_tables_sync(
        self, samples: Sequence[StorageSample], *, lease: _OperationLease
    ) -> None:
        with self._ddl_lock, self._borrow_connection(lease=lease) as (connection, _):
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
                    (names.database, names.database_identity_sha256, (sample.wind_farm_id,)),
                    (
                        names.stable,
                        names.stable_identity_sha256,
                        (
                            sample.wind_farm_id,
                            sample.device_type,
                            sample.point_table_id,
                            sample.data_level.value,
                            SCHEMA_FINGERPRINT,
                        ),
                    ),
                    (
                        names.table,
                        names.table_identity_sha256,
                        (
                            sample.wind_farm_id,
                            sample.device_type,
                            sample.device_id,
                            sample.point_table_id,
                            sample.data_level.value,
                            SCHEMA_FINGERPRINT,
                        ),
                    ),
                )
                registered = self._metadata.list_semantic_pins().get(sample.point_table_id)
                if registered is None:
                    raise TDengineSchemaConflictError(
                        "physical table has no durable semantic closure ownership"
                    )
                closure_digest = hashlib.sha256(
                    self._identity_json(
                        (
                            sample.point_table_id,
                            *registered[0],
                            *(registered[1] or ("", "")),
                        )
                    ).encode("utf-8")
                ).hexdigest()
                registrations = [
                    self._metadata.reserve_object(
                        name,
                        digest,
                        self._identity_json(identity),
                        SCHEMA_FINGERPRINT,
                        physical_database=names.database,
                        stable_name=names.stable if name != names.database else "",
                        table_name=names.table if name == names.table else "",
                        closure_hash=(
                            names.database_identity_sha256
                            if name == names.database
                            else closure_digest
                        ),
                    )
                    for name, digest, identity in identities
                ]
                connection.execute(
                    create_database_sql(
                        names,
                        retention,
                        cachemodel=self._config.tdengine.database.cachemodel,
                        vgroups=self._config.tdengine.database.vgroups,
                        buffer_mb=self._config.tdengine.database.buffer_mb,
                        replica=self._config.tdengine.database.replica,
                        comp=self._config.tdengine.database.comp,
                        wal_level=self._config.tdengine.database.wal_level,
                        wal_fsync_period_ms=self._config.tdengine.database.wal_fsync_period_ms,
                    )
                )
                # IF NOT EXISTS cannot mutate existing KEEP. Changes are controlled.
                self._reconcile_database_retention(connection, names, retention)
                connection.execute(
                    create_stable_sql(names).replace(
                        "value_u64 UBIGINT", "value_u64 BIGINT UNSIGNED"
                    )
                )
                connection.execute(
                    create_table_sql(
                        names,
                        identity_hash=names.table_identity_sha256,
                        point_table_hash=hashlib.sha256(
                            sample.point_table_id.encode("utf-8")
                        ).hexdigest(),
                        device_hash=hashlib.sha256(sample.device_id.encode("utf-8")).hexdigest(),
                        level_tag=_LEVEL[sample.data_level],
                    )
                )
                # CREATE IF NOT EXISTS is not verification. A mismatched existing
                # object is a hard stop; never mark CREATING as READY by guessing.
                self._verify_table(
                    connection,
                    names,
                    sample,
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

    def _reconcile_database_retention(
        self,
        connection: _TaosConnection,
        names: StorageObjectNames,
        retention: DatabaseRetention,
    ) -> None:
        """Only increase KEEP under explicit policy; shrink is fail-closed."""
        rows = list(connection.query(f"SHOW CREATE DATABASE {names.database}"))
        if len(rows) != 1 or len(rows[0]) < 2:
            raise TDengineSchemaConflictError("TDengine database readback failed")
        normalized = "".join(
            str(rows[0][-1]).upper().replace(chr(96), "").replace("'", "").replace('"', "").split()
        )
        if "PRECISIONUS" not in normalized:
            raise TDengineSchemaConflictError("database precision differs from us")
        expected_cachemodel = self._config.tdengine.database.cachemodel.upper()
        if f"CACHEMODEL{expected_cachemodel}" not in normalized:
            raise TDengineSchemaConflictError("database CACHEMODEL differs from configuration")
        database = self._config.tdengine.database
        for name, value in (
            ("VGROUPS", database.vgroups),
            ("BUFFER", database.buffer_mb),
            ("REPLICA", database.replica),
            ("COMP", database.comp),
            ("WAL_LEVEL", database.wal_level),
            ("WAL_FSYNC_PERIOD", database.wal_fsync_period_ms),
        ):
            if f"{name}{value}" not in normalized:
                raise RuntimeError(f"TDengine database parameter mismatch: {name}")
        duration = re.search(r"DURATION([1-9][0-9]*)D", normalized)
        keep = re.search(r"KEEP([1-9][0-9]*)D", normalized)
        if duration is None or keep is None:
            raise TDengineSchemaConflictError("database DURATION/KEEP unavailable")
        if int(duration.group(1)) != retention.duration_days:
            raise TDengineSchemaConflictError(
                "changing database DURATION requires a validated migration"
            )
        current_keep = int(keep.group(1))
        if current_keep == retention.keep_days:
            return
        if current_keep > retention.keep_days:
            raise TDengineSchemaConflictError(
                "retention shrink needs one-use authenticated database-specific evidence"
            )
        if not self._config.reconciliation.auto_alter:
            raise TDengineSchemaConflictError(
                "database KEEP increase requires reconciliation.auto_alter=true"
            )
        connection.execute(f"ALTER DATABASE {names.database} KEEP {retention.keep_days}d")
        after = list(connection.query(f"SHOW CREATE DATABASE {names.database}"))
        if len(after) != 1 or len(after[0]) < 2:
            raise TDengineSchemaConflictError("database KEEP ALTER readback failed")
        checked = "".join(
            str(after[0][-1]).upper().replace(chr(96), "").replace("'", "").replace('"', "").split()
        )
        if f"KEEP{retention.keep_days}D" not in checked:
            raise TDengineSchemaConflictError("database KEEP ALTER did not converge")

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
            raise TDengineSchemaConflictError("missing TDengine data columns")
        for observed, (wanted_name, wanted_type) in zip(columns, wanted, strict=False):
            if str(observed[0]).lower() != wanted_name:
                raise TDengineSchemaConflictError(f"mismatched TDengine column: {wanted_name}")
            expected_base = (
                wanted_type.split("(")[0].split(" ")[0].replace("UBIGINT", "BIGINT UNSIGNED")
            )
            if str(observed[1]).upper() != expected_base:
                raise TDengineSchemaConflictError(f"mismatched TDengine type: {wanted_name}")
            declared_length = re.search(r"\((\d+)\)", wanted_type)
            if declared_length is not None:
                if len(observed) < 3:
                    raise TDengineSchemaConflictError(
                        f"TDengine column length unavailable: {wanted_name}"
                    )
                try:
                    actual_length = int(observed[2])
                except (TypeError, ValueError) as exc:
                    raise TDengineSchemaConflictError(
                        f"invalid TDengine column length: {wanted_name}"
                    ) from exc
                if actual_length != int(declared_length.group(1)):
                    raise TDengineSchemaConflictError(
                        f"mismatched TDengine column length: {wanted_name}"
                    )

        tag_rows = list(connection.query(f"SHOW TAGS FROM {names.database}.{names.table}"))
        tag_map = {str(row[3]): str(row[5]) for row in tag_rows}
        expected = {
            "identity_hash": names.table_identity_sha256,
            "point_table_hash": expected_point_hash,
            "device_hash": expected_device_hash,
            "level_tag": str(_LEVEL[sample.data_level]),
        }
        if any(tag_map.get(name) != value for name, value in expected.items()):
            raise TDengineSchemaConflictError("TDengine table TAG identity mismatch")
        if len(tag_map) != len(expected_tags()):
            raise TDengineSchemaConflictError("TDengine TAG count mismatch")

    async def fetch_content_hashes(
        self, samples: Sequence[StorageSample]
    ) -> dict[str, frozenset[str]]:
        return await self._run_bounded(self._fetch_content_hashes_sync, samples=samples)

    def _fetch_content_hashes_sync(
        self, samples: Sequence[StorageSample], *, lease: _OperationLease
    ) -> dict[str, frozenset[str]]:
        """Batch readback using bounded time ranges and generated SHA-256 keys."""
        grouped: dict[str, list[StorageSample]] = defaultdict(list)
        result: dict[str, set[str]] = {}
        for sample in samples:
            names = get_object_names(sample, SCHEMA_FINGERPRINT)
            grouped[f"{names.database}.{names.table}"].append(sample)
            result.setdefault(sample.logical_key, set())
        with self._borrow_connection(lease=lease) as (connection, _):
            for table, entries in grouped.items():
                # TDengine can push the time predicate to partition pruning.
                # Keep each IN list short and distinct to bound statement size.
                unique = list({entry.logical_key: entry for entry in entries}.values())
                for begin in range(0, len(unique), 512):
                    chunk = unique[begin : begin + 512]
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
                            raise TDengineSchemaConflictError(
                                "unexpected logical key from database"
                            )
                        if not _HEX.fullmatch(value_hash):
                            raise TDengineSchemaConflictError("invalid content hash in database")
                        result[logical_key].add(value_hash)
        return {key: frozenset(values) for key, values in result.items()}

    async def insert_batch(self, samples: Sequence[StorageSample]) -> None:
        await self._run_bounded(self._insert_batch_sync, samples=samples)

    def _insert_batch_sync(
        self, samples: Sequence[StorageSample], *, lease: _OperationLease
    ) -> None:
        if not samples:
            return
        groups: dict[str, list[StorageSample]] = defaultdict(list)
        for sample in samples:
            names = get_object_names(sample, SCHEMA_FINGERPRINT)
            groups[f"{names.database}.{names.table}"].append(sample)
        with self._borrow_connection(lease=lease) as (connection, driver):
            for table_name, batch in groups.items():
                if len(batch) > self._config.tdengine.ingestion.batch_max_rows:
                    raise ValueError("TDengine batch record count exceeds configured maximum")
                statement = connection.stmt2_statement()
                try:
                    placeholders = ", ".join("?" for _ in expected_columns())
                    statement.prepare(f"INSERT INTO {table_name} VALUES ({placeholders})")
                    binding = driver.stmt2_bind_param_view(
                        table_name=None, tags=None, columns=_columns(batch, driver)
                    )
                    statement.bind([binding])
                    affected = statement.execute()
                    # TDengine duplicate-key replay may report a different row count.
                    # Never infer success from affected rows: caller verifies every key.
                    if affected < 0:
                        raise RuntimeError("TDengine reported a negative affected count")
                finally:
                    statement.close()

    async def ensure_wide_table(
        self,
        schema: WideSchema,
        *,
        wind_farm_id: str,
        device_id: str,
        device_type: str,
    ) -> None:
        """Create/verify a complete immutable wide-table family on first use."""
        await self._run_bounded(
            self._ensure_wide_table_sync,
            schema=schema,
            wind_farm_id=wind_farm_id,
            device_id=device_id,
            device_type=device_type,
        )

    def _ensure_wide_table_sync(
        self,
        *,
        schema: WideSchema,
        wind_farm_id: str,
        device_id: str,
        device_type: str,
        lease: _OperationLease,
    ) -> None:
        database_name = schema.database_name(wind_farm_id)
        stable_name = schema.supertable_name()
        table_name = schema.subtable_name(device_id)
        if len(device_id.encode("utf-8")) > 128 or len(device_type.encode("utf-8")) > 64:
            raise ValueError("device TAG exceeds physical width")
        with self._ddl_lock, self._borrow_connection(lease=lease) as (connection, _):
            if table_name in self._verified_tables:
                return
            pinned = self._metadata.list_semantic_pins().get(schema.point_table_id)
            if pinned is None:
                raise TDengineSchemaConflictError(
                    "wide table lacks an immutable historical PointTable closure"
                )
            if pinned[0][1] != schema.point_table_version:
                raise TDengineSchemaConflictError(
                    "wide table PointTable version differs from authorized closure"
                )

            def identity(*parts: str) -> tuple[str, str]:
                content = self._identity_json(parts)
                return content, hashlib.sha256(content.encode("utf-8")).hexdigest()

            db_json, db_hash = identity(wind_farm_id)
            stable_json, stable_hash = identity(
                wind_farm_id, schema.point_table_id, schema.point_table_version,
                schema.data_level.value, schema.fingerprint
            )
            table_json, table_hash = identity(
                wind_farm_id, device_id, schema.point_table_id,
                schema.point_table_version, schema.data_level.value, schema.fingerprint
            )
            registrations = [
                self._metadata.reserve_object(
                    name, digest, description, schema.fingerprint,
                    physical_database=database_name,
                    stable_name=stable_name if name != database_name else "",
                    table_name=table_name if name == table_name else "",
                    closure_hash=db_hash if name == database_name else stable_hash,
                )
                for name, digest, description in (
                    (database_name, db_hash, db_json),
                    (stable_name, stable_hash, stable_json),
                    (table_name, table_hash, table_json),
                )
            ]
            names = StorageObjectNames(
                database=database_name,
                stable=stable_name,
                table=table_name,
                database_identity_sha256=db_hash,
                stable_identity_sha256=stable_hash,
                table_identity_sha256=table_hash,
            )
            retention = DatabaseRetention.parse(
                duration=self._config.tdengine.database.duration,
                keep=self._config.tdengine.database.keep,
            )
            lease.remaining()
            connection.execute(
                create_database_sql(
                    names,
                    retention,
                    cachemodel=self._config.tdengine.database.cachemodel,
                    vgroups=self._config.tdengine.database.vgroups,
                    buffer_mb=self._config.tdengine.database.buffer_mb,
                    replica=self._config.tdengine.database.replica,
                    comp=self._config.tdengine.database.comp,
                    wal_level=self._config.tdengine.database.wal_level,
                    wal_fsync_period_ms=self._config.tdengine.database.wal_fsync_period_ms,
                )
            )
            self._reconcile_database_retention(connection, names, retention)
            lease.remaining()
            connection.execute(stable_ddl(schema, database=database_name))
            lease.remaining()
            connection.execute(
                subtable_ddl(
                    schema, database=database_name,
                    device_id=device_id, device_type=device_type,
                )
            )
            self._verify_wide_table(
                connection,
                names=names,
                schema=schema,
                device_id=device_id,
                device_type=device_type,
            )
            for registration in registrations:
                self._metadata.mark_ready(registration, verified=True)
            self._verified_tables.add(table_name)

    @staticmethod
    def _verify_wide_table(
        connection: _TaosConnection,
        *,
        names: StorageObjectNames,
        schema: WideSchema,
        device_id: str,
        device_type: str,
    ) -> None:
        """CREATE IF NOT EXISTS is not proof of type, length, or table ownership."""
        observed = list(connection.query(f"DESCRIBE {names.database}.{names.table}"))
        wanted = [
            ("ts", "TIMESTAMP"),
            *((column.column_name, column.sql_type) for column in schema.columns),
            ("quality", f"VARBINARY({schema.quality_bytes})"),
        ]
        if len(observed) != len(wanted) + 5:
            raise TDengineSchemaConflictError("wide-table column/tag count differs")
        for row, (name, sql_type) in zip(observed, wanted, strict=False):
            if str(row[0]).lower() != name:
                raise TDengineSchemaConflictError("wide-table column order differs")
            match = re.fullmatch(r"(.+?)\\(([0-9]+)\\)", sql_type)
            expected_type = sql_type if match is None else match.group(1)
            if str(row[1]).upper() != expected_type:
                raise TDengineSchemaConflictError(f"wide-table type mismatch: {name}")
            if match is not None and int(row[2]) != int(match.group(2)):
                raise TDengineSchemaConflictError(f"wide-table width mismatch: {name}")
        tag_rows = list(connection.query(f"SHOW TAGS FROM {names.database}.{names.table}"))
        tags = {str(row[3]): str(row[5]) for row in tag_rows}
        wanted_tags = {
            "device_id": device_id,
            "device_type": device_type,
            "point_table_id": schema.point_table_id,
            "data_level": str(_LEVEL[DataLevel(schema.data_level.value)]),
            "schema_fingerprint": schema.fingerprint,
        }
        if len(tags) != len(wanted_tags) or tags != wanted_tags:
            raise TDengineSchemaConflictError("wide-table immutable TAG identity differs")

    async def insert_wide_batch(
        self,
        schema: WideSchema,
        records: Sequence[WideRecord],
    ) -> None:
        """Perform a device-wide STMT2 write under the existing bounded I/O lease.

        The caller must first create and verify the target physical table and
        immutable ownership metadata. This method never issues per-point SELECT.
        """
        if not records:
            return
        first = records[0]
        if any(
            row.wind_farm_id != first.wind_farm_id or row.device_id != first.device_id
            for row in records
        ):
            raise ValueError("wide STMT2 batches must contain exactly one device")
        name = schema.subtable_name(first.device_id)
        with self._lock:
            if name not in self._verified_tables:
                raise TDengineSchemaConflictError("wide target has not been verified")
        await self._run_bounded(
            self._insert_wide_batch_sync, schema=schema, records=tuple(records)
        )

    def _insert_wide_batch_sync(
        self,
        *,
        schema: WideSchema,
        records: Sequence[WideRecord],
        lease: _OperationLease,
    ) -> None:
        first = records[0]
        name = (
            f"{schema.database_name(first.wind_farm_id)}."
            f"{schema.subtable_name(first.device_id)}"
        )
        with self._borrow_connection(lease=lease) as (connection, driver):
            lease.remaining()
            write_wide_stmt2(
                connection, driver, table_name=name, schema=schema, records=records
            )

    async def fetch_history(
        self,
        filter_: HistoryFilter,
        *,
        after: tuple[int, str, str] | None,
        limit: int,
    ) -> list[HistoryRow]:
        """Read only previously registered physical tables and bounded result pages."""
        return await self._run_bounded(self._fetch_history_sync, filter_, after, limit)

    @staticmethod
    def _sql_filter(value: str) -> str:
        """Escape a validated UTF-8 value as one SQL string literal."""
        if not value or len(value.encode("utf-8")) > 256:
            raise ValueError("SQL query identity must contain 1..256 UTF-8 bytes")
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("SQL query identity contains control characters")
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _datetime_us(value: object) -> int:
        from datetime import UTC, datetime

        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value)
            except ValueError as exc:
                raise TDengineSchemaConflictError(
                    "TDengine returned an invalid timestamp string"
                ) from exc
        if isinstance(value, datetime):
            if value.tzinfo is None:
                raise TDengineSchemaConflictError("TDengine returned a timezone-naive timestamp")
            moment = value.astimezone(UTC)
            epoch = datetime(1970, 1, 1, tzinfo=UTC)
            delta = moment - epoch
            return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds
        raise TDengineSchemaConflictError("TDengine returned an invalid timestamp representation")

    @staticmethod
    def _decode_record(
        row: Sequence[object],
        identity: tuple[str, ...],
    ) -> HistoryRow:
        if len(row) != 17 or len(identity) != 6:
            raise TDengineSchemaConflictError("TDengine history row/schema shape mismatch")
        kind = _stored_int(row[7])
        value_columns = {2: 8, 3: 9, 4: 10, 5: 11, 6: 12, 7: 13}
        if kind == 1:
            value: int | float | bool | str | bytes | None = None
            if any(row[idx] is not None for idx in range(8, 14)):
                raise TDengineSchemaConflictError("MISSING row has nonempty value columns")
        else:
            index = value_columns.get(kind)
            if index is None:
                raise TDengineSchemaConflictError("unknown TDengine value kind")
            if any(row[idx] is not None for idx in range(8, 14) if idx != index):
                raise TDengineSchemaConflictError("multiple TDengine value columns are set")
            raw = row[index]
            if raw is None:
                raise TDengineSchemaConflictError("typed row has NULL value")
            if kind in (2, 3):
                if isinstance(raw, bool) or not isinstance(raw, int):
                    raise TDengineSchemaConflictError("stored integer lost its type")
                value = raw
            elif kind == 4:
                if isinstance(raw, bool) or not isinstance(raw, float | int):
                    raise TDengineSchemaConflictError("stored DOUBLE lost its type")
                value = float(raw)
            elif kind == 5:
                if not isinstance(raw, bool):
                    raise TDengineSchemaConflictError("stored BOOL lost its type")
                value = raw
            elif kind == 6:
                if not isinstance(raw, str):
                    raise TDengineSchemaConflictError("stored TEXT lost its type")
                value = raw
            else:
                if not isinstance(raw, bytes | bytearray | memoryview):
                    raise TDengineSchemaConflictError("stored BYTES lost its type")
                value = bytes(raw)
        quality = {1: "good", 2: "bad", 3: "uncertain"}.get(_stored_int(row[14]))
        if quality is None:
            raise TDengineSchemaConflictError("unknown stored quality")
        logical = str(row[1])
        content = str(row[2])
        if not _HEX.fullmatch(logical) or not _HEX.fullmatch(content):
            raise TDengineSchemaConflictError("invalid stored logical/content digest")
        return HistoryRow(
            ts_us=TDengineBackend._datetime_us(row[0]),
            logical_key=logical,
            content_sha256=content,
            identity=identity,
            sample_id=str(row[3]),
            point_id=str(row[4]),
            business_point_id=str(row[5]),
            value_kind=kind,
            value=value,
            quality=quality,
            publish_time_us=TDengineBackend._datetime_us(row[15]),
            receive_time_us=(
                TDengineBackend._datetime_us(row[16]) if row[16] is not None else None
            ),
        )

    def _fetch_history_sync(
        self,
        filter_: HistoryFilter,
        after: tuple[int, str, str] | None,
        limit: int,
        *,
        lease: _OperationLease,
    ) -> list[HistoryRow]:
        """Keyset-scan managed tables; exclude every sample conflict candidate."""
        if not 1 <= limit <= 1001:
            raise ValueError("SQL query limit must be 1..1001")
        db_hash = hashlib.sha256(
            json.dumps(
                (filter_.wind_farm_id,),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        database = f"wh_{db_hash[:24]}"
        registrations = self._metadata.list_tables(database=database)
        if len(registrations) > 1024:
            raise RuntimeError("history query exceeds managed-table budget")
        output: list[HistoryRow] = []
        columns = (
            "ts, logical_key, content_sha256, sample_id, point_id, "
            "business_point_id, data_level, value_kind, value_i64, "
            "value_u64, value_f64, value_bool, value_text, "
            "payload_bytes, quality, publish_time, receive_time"
        )
        with self._borrow_connection(lease=lease) as (connection, _):
            for entry in registrations:
                if (
                    re.fullmatch(r"tb_[0-9a-f]{24}", entry.name) is None
                    or entry.schema_fingerprint != SCHEMA_FINGERPRINT
                ):
                    raise TDengineSchemaConflictError("untrusted historical physical table name")
                try:
                    decoded = json.loads(entry.identity_json)
                except (ValueError, TypeError) as exc:
                    raise TDengineSchemaConflictError("corrupt registered table identity") from exc
                if (
                    not isinstance(decoded, list)
                    or len(decoded) != 6
                    or any(not isinstance(item, str) for item in decoded)
                ):
                    raise TDengineSchemaConflictError("corrupt registered table identity")
                identity = tuple(decoded)
                if (
                    identity[0] != filter_.wind_farm_id
                    or identity[4] != filter_.data_level.value
                    or (filter_.device_type and identity[1] != filter_.device_type)
                    or (filter_.device_id and identity[2] != filter_.device_id)
                    or (filter_.point_table_id and identity[3] != filter_.point_table_id)
                ):
                    continue
                table = f"{database}.{entry.name}"
                predicates = [
                    f"ts >= {filter_.start_us}",
                    f"ts < {filter_.end_us}",
                ]
                if filter_.point_id:
                    predicates.append(f"point_id = {self._sql_filter(filter_.point_id)}")
                if filter_.business_point_id:
                    predicates.append(
                        "business_point_id = " f"{self._sql_filter(filter_.business_point_id)}"
                    )
                if filter_.qualities:
                    quality_values = ",".join(str(_QUALITY[q]) for q in filter_.qualities)
                    predicates.append(f"quality IN ({quality_values})")

                cursor = after
                scanned = 0
                valid: list[HistoryRow] = []
                # Scan at most 4096 physical candidates per table and page.
                while len(valid) < limit:
                    remaining = limit - len(valid)
                    fetch_size = min(256, max(32, remaining + 16))
                    where = list(predicates)
                    if cursor is not None:
                        timestamp, logical, content = cursor
                        if not _HEX.fullmatch(logical) or not _HEX.fullmatch(content):
                            raise ValueError("invalid history cursor digest")
                        where.append(
                            f"(ts > {timestamp} OR "
                            f"(ts = {timestamp} AND "
                            f"(logical_key > '{logical}' OR "
                            f"(logical_key = '{logical}' AND "
                            f"content_sha256 > '{content}'))))"
                        )
                    sql = (
                        f"SELECT {columns} FROM {table} "
                        f"WHERE {' AND '.join(where)} "
                        "ORDER BY ts, logical_key, content_sha256 "
                        f"LIMIT {fetch_size}"
                    )
                    raw_rows = list(connection.query(sql))
                    if len(raw_rows) > fetch_size:
                        raise TDengineSchemaConflictError("TDengine exceeded query limit")
                    if not raw_rows:
                        break
                    scanned += len(raw_rows)
                    if scanned > 4096:
                        raise TDengineSchemaConflictError(
                            "too many conflicting candidates in one history page"
                        )
                    candidates: list[HistoryRow] = []
                    for row in raw_rows:
                        if int(row[6]) != _LEVEL[filter_.data_level]:
                            raise TDengineSchemaConflictError("row level differs from table")
                        candidates.append(self._decode_record(row, identity))
                    if any(
                        a.sort_key >= b.sort_key
                        for a, b in zip(candidates, candidates[1:], strict=False)
                    ):
                        raise TDengineSchemaConflictError("history order not monotonic")
                    cursor = candidates[-1].sort_key
                    conflicts: set[str] = set()
                    keys = sorted({item.logical_key for item in candidates})
                    for begin in range(0, len(keys), 128):
                        part = keys[begin : begin + 128]
                        literals = ",".join(f"'{key}'" for key in part)
                        observed: dict[str, set[str]] = defaultdict(set)
                        for logical, digest in connection.query(
                            f"SELECT logical_key, content_sha256 FROM {table} "
                            f"WHERE logical_key IN ({literals})"
                        ):
                            key = str(logical)
                            content_hash = str(digest)
                            if key not in part or not _HEX.fullmatch(content_hash):
                                raise TDengineSchemaConflictError(
                                    "invalid logical-key verification response"
                                )
                            observed[key].add(content_hash)
                        if any(not observed.get(key) for key in part):
                            raise TDengineSchemaConflictError(
                                "cannot prove all logical-key candidates"
                            )
                        conflicts.update(key for key, hashes in observed.items() if len(hashes) > 1)
                    if conflicts:
                        _LOG.warning(
                            "Storage history excluded %d conflicting logical keys",
                            len(conflicts),
                        )
                    valid.extend(
                        candidate
                        for candidate in candidates
                        if candidate.logical_key not in conflicts
                    )
                    if len(raw_rows) < fetch_size:
                        break
                output.extend(valid[:limit])
        output.sort(key=lambda row: row.sort_key)
        return output[:limit]
