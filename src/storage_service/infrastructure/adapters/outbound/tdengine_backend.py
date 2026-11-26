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
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from importlib import import_module
from typing import Any, Protocol, TypeVar, cast

from storage_service.application.config import StorageConfig
from storage_service.application.wide_query import WideHistoryFilter, WideHistoryRow
from storage_service.domain.quality_bitmap import unpack_quality_bitmap
from storage_service.domain.errors import TDengineSchemaConflictError
from storage_service.domain.naming import StorageObjectNames
from storage_service.domain.wide_record import WideDataLevel, WideRecord
from storage_service.domain.wide_schema import WideSchema
from storage_service.infrastructure.adapters.outbound.metadata_store import (
    MetadataConflictError,
    MetadataStore,
)
from storage_service.infrastructure.adapters.outbound.wide_ddl import stable_ddl, subtable_ddl
from storage_service.infrastructure.adapters.outbound.wide_stmt2 import write_wide_stmt2
from storage_service.infrastructure.adapters.outbound.tdengine_schema import (
    DatabaseRetention,
    create_database_sql,
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
_LOG = logging.getLogger(__name__)
_LEVEL = {WideDataLevel.RAW: 1, WideDataLevel.PROTOCOL: 2, WideDataLevel.BUSINESS: 3}
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
        self._wide_unknown_tables: set[str] = set()
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

    @staticmethod
    def _identity_json(values: tuple[str, ...]) -> str:
        return json.dumps(values, ensure_ascii=False, separators=(",", ":"))

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

    async def ensure_wide_table(
        self,
        schema: WideSchema,
        *,
        wind_farm_id: str,
        device_id: str,
        device_type: str,
    ) -> None:
        """Create/verify a complete immutable wide-table family on first use."""
        target = (
            f"{schema.database_name(wind_farm_id)}."
            f"{schema.subtable_name(device_id)}"
        )
        with self._lock:
            if target in self._wide_unknown_tables:
                raise TDengineSchemaConflictError(
                    "wide DDL I/O is unresolved; require process isolation"
                )
        try:
            await self._run_bounded(
                self._ensure_wide_table_sync,
                schema=schema,
                wind_farm_id=wind_farm_id,
                device_id=device_id,
                device_type=device_type,
            )
        except TDengineSchemaConflictError:
            raise
        except BaseException:
            # Timed-out DDL may still own the connector thread.
            with self._lock:
                self._wide_unknown_tables.add(target)
            raise

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
        qualified_name = f"{database_name}.{table_name}"
        if len(device_id.encode("utf-8")) > 128 or len(device_type.encode("utf-8")) > 64:
            raise ValueError("device TAG exceeds physical width")
        with self._ddl_lock, self._borrow_connection(lease=lease) as (connection, _):
            if qualified_name in self._verified_tables:
                return
            pinned = self._metadata.list_semantic_pins().get(schema.point_table_id)
            if pinned is None:
                raise TDengineSchemaConflictError(
                    "wide table lacks an immutable historical PointTable closure"
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
                    metadata_name, digest, description,
                    "windhub-database-v1" if kind == "database" else schema.fingerprint,
                    physical_database=database_name,
                    stable_name=stable_name if kind != "database" else "",
                    table_name=table_name if kind == "table" else "",
                    closure_hash=db_hash if kind == "database" else stable_hash,
                )
                for kind, metadata_name, digest, description in (
                    ("database", database_name, db_hash, db_json),
                    ("stable", f"{database_name}.{stable_name}", stable_hash, stable_json),
                    ("table", f"{database_name}.{table_name}", table_hash, table_json),
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
            with self._lock:
                self._verified_tables.add(qualified_name)

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
        if len(observed) not in (len(wanted), len(wanted) + 5):
            raise TDengineSchemaConflictError("wide-table column/tag count differs")
        for row, (name, sql_type) in zip(observed, wanted, strict=False):
            if str(row[0]).lower() != name:
                raise TDengineSchemaConflictError("wide-table column order differs")
            match = re.fullmatch(r"(.+?)\(([0-9]+)\)", sql_type)
            expected_type = sql_type if match is None else match.group(1)
            if str(row[1]).upper() != expected_type:
                raise TDengineSchemaConflictError(f"wide-table type mismatch: {name}")
            if match is not None:
                if len(row) < 3 or not str(row[2]).isdigit():
                    raise TDengineSchemaConflictError(f"wide-table width unknown: {name}")
                if int(row[2]) != int(match.group(2)):
                    raise TDengineSchemaConflictError(f"wide-table width mismatch: {name}")
        tag_rows = list(connection.query(f"SHOW TAGS FROM {names.database}.{names.table}"))
        expected_tags = (
            ("device_id", "VARCHAR(128)", device_id),
            ("device_type", "VARCHAR(64)", device_type),
            ("point_table_id", "VARCHAR(192)", schema.point_table_id),
            ("data_level", "TINYINT", str(_LEVEL[schema.data_level])),
            ("schema_fingerprint", "VARCHAR(64)", schema.fingerprint),
        )
        if len(tag_rows) != len(expected_tags):
            raise TDengineSchemaConflictError("wide-table immutable TAG count differs")
        for row, (tag_name, tag_type, tag_value) in zip(
            tag_rows, expected_tags, strict=True
        ):
            if len(row) < 6:
                raise TDengineSchemaConflictError("wide-table TAG metadata is incomplete")
            table, database, stable = (str(item) for item in row[:3])
            if (
                table != names.table
                or database != names.database
                or stable != names.stable
                or str(row[3]).lower() != tag_name
                or str(row[4]).upper() != tag_type
                or str(row[5]) != tag_value
            ):
                raise TDengineSchemaConflictError(
                    f"wide-table TAG/parent ownership mismatch: {tag_name}"
                )

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
        limits = self._config.tdengine.ingestion
        if (
            len(records) > limits.batch_max_rows
            or len(records) * schema.declared_row_bytes > limits.batch_max_bytes
        ):
            raise ValueError("wide STMT2 batch exceeds physical row/byte budget")
        first = records[0]
        if any(
            row.wind_farm_id != first.wind_farm_id or row.device_id != first.device_id
            for row in records
        ):
            raise ValueError("wide STMT2 batches must contain exactly one device")
        name = (
            f"{schema.database_name(first.wind_farm_id)}."
            f"{schema.subtable_name(first.device_id)}"
        )
        with self._lock:
            if name not in self._verified_tables:
                raise TDengineSchemaConflictError("wide target has not been verified")
            if name in self._wide_unknown_tables:
                raise TDengineSchemaConflictError(
                    "prior wide I/O remains UNKNOWN; restart only after old process isolation"
                )
        try:
            await self._run_bounded(
                self._insert_wide_batch_sync, schema=schema, records=tuple(records)
            )
        except BaseException:
            # A connector exception is not proof that no rows were persisted.
            # Preserve the device fence until the old process/I/O is isolated.
            with self._lock:
                self._wide_unknown_tables.add(name)
            raise

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

    async def verify_existing_wide_table(
        self,
        schema: WideSchema,
        *,
        wind_farm_id: str,
        device_id: str,
        device_type: str,
    ) -> None:
        """Rebuild safe read cache only from persisted ownership and live schema."""
        name = (
            f"{schema.database_name(wind_farm_id)}."
            f"{schema.subtable_name(device_id)}"
        )
        with self._lock:
            if name in self._wide_unknown_tables:
                raise TDengineSchemaConflictError("wide table has unresolved writes")
            if name in self._verified_tables:
                return
        await self._run_bounded(
            self._verify_existing_wide_table_sync,
            schema=schema, wind_farm_id=wind_farm_id,
            device_id=device_id, device_type=device_type,
        )

    def _verify_existing_wide_table_sync(
        self,
        *,
        schema: WideSchema,
        wind_farm_id: str,
        device_id: str,
        device_type: str,
        lease: _OperationLease,
    ) -> None:
        database = schema.database_name(wind_farm_id)
        stable = schema.supertable_name()
        table = schema.subtable_name(device_id)
        full_name = f"{database}.{table}"
        description = self._identity_json(
            (
                wind_farm_id, device_id, schema.point_table_id,
                schema.point_table_version, schema.data_level.value,
                schema.fingerprint,
            )
        )
        digest = hashlib.sha256(description.encode("utf-8")).hexdigest()
        try:
            self._metadata.require_ready_wide_table(
                name=full_name, fingerprint=schema.fingerprint,
                identity_sha256=digest,
            )
        except MetadataConflictError as exc:
            raise TDengineSchemaConflictError(
                "historical wide table ownership cannot be proven"
            ) from exc
        names = StorageObjectNames(
            database=database, stable=stable, table=table,
            database_identity_sha256="",
            stable_identity_sha256="",
            table_identity_sha256=digest,
        )
        with self._ddl_lock, self._borrow_connection(lease=lease) as (connection, _):
            lease.remaining()
            self._verify_wide_table(
                connection, names=names, schema=schema,
                device_id=device_id, device_type=device_type,
            )
            with self._lock:
                if full_name in self._wide_unknown_tables:
                    raise TDengineSchemaConflictError("wide table became uncertain")
                self._verified_tables.add(full_name)

    async def fetch_wide_history(
        self,
        schema: WideSchema,
        query: WideHistoryFilter,
    ) -> tuple[WideHistoryRow, ...]:
        """Fetch exact physical fields without merging versions or filling gaps."""
        query.validate(schema, maximum=self._config.query.max_page_size + 1)
        return await self._run_bounded(
            self._fetch_wide_history_sync, schema=schema, query=query
        )

    def _fetch_wide_history_sync(
        self,
        *,
        schema: WideSchema,
        query: WideHistoryFilter,
        lease: _OperationLease,
    ) -> tuple[WideHistoryRow, ...]:
        name = (
            f"{schema.database_name(query.wind_farm_id)}."
            f"{schema.subtable_name(query.device_id)}"
        )
        with self._lock:
            if name not in self._verified_tables or name in self._wide_unknown_tables:
                raise TDengineSchemaConflictError(
                    "historical wide table is unverified or has unresolved writes"
                )
        by_id = {column.point_id: column for column in schema.columns}
        projected = tuple(by_id[point_id] for point_id in query.point_ids)
        column_sql = ", ".join(column.column_name for column in projected)
        filters = [
            f"ts >= {query.start_time_us}",
            f"ts < {query.end_time_us}",
        ]
        if query.after_time_us is not None:
            filters.append(f"ts > {query.after_time_us}")
        sql = (
            f"SELECT ts, {column_sql}, quality FROM {name} "
            f"WHERE {' AND '.join(filters)} ORDER BY ts ASC LIMIT {query.limit}"
        )
        indexes = {
            column.point_id: index for index, column in enumerate(schema.columns)
        }
        with self._borrow_connection(lease=lease) as (connection, _driver):
            lease.remaining()
            records = list(connection.query(sql))
        result: list[WideHistoryRow] = []
        for raw in records:
            if len(raw) != len(projected) + 2:
                raise TDengineSchemaConflictError("wide query returned unexpected row width")
            timestamp = self._datetime_us(raw[0])
            bits = raw[-1]
            if not isinstance(bits, bytes | bytearray | memoryview):
                raise TDengineSchemaConflictError("wide quality bitmap is not binary")
            try:
                qualities = unpack_quality_bitmap(bytes(bits), len(schema.columns))
            except ValueError as exc:
                raise TDengineSchemaConflictError("corrupt wide quality bitmap") from exc
            fields = tuple(
                (column.point_id, raw[i + 1], qualities[indexes[column.point_id]])
                for i, column in enumerate(projected)
            )
            result.append(WideHistoryRow(timestamp, fields))
        return tuple(result)

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

