"""真实 TDengine 多设备 STMT2、statement 缓存与 UNKNOWN offset 隔离。"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest

from storage_service.application.config import StorageConfig, load_storage_config
from storage_service.application.ingestion_contract import IngestionBlockedError, KafkaRecord
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.offsets import PartitionOffsets
from storage_service.application.wide_ingestion import WideIngestionService
from storage_service.application.wide_query import WideHistoryFilter
from storage_service.domain.errors import TDengineSchemaConflictError
from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_record import WideDataLevel, WideIdentity, WideRecord
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from storage_service.infrastructure.adapters.outbound.tdengine_backend import TDengineBackend
from tests.fixtures.services.r5_stack import R5Stack, docker_available, down_stack, up_stack

ROOT = Path(__file__).resolve().parents[3]
POINT_COUNT = 500
DEVICE_TYPE = "wind_turbine"
CACHE_LIMIT = 32
SAMPLE_TIME_US = int(datetime(2026, 11, 26, tzinfo=UTC).timestamp() * 1_000_000)
pytestmark = [pytest.mark.integration, pytest.mark.real_service, pytest.mark.docker]


class _StatementProbe:
    """透明转发真实 STMT2；钩子只负责已声明的故障注入。"""

    def __init__(self, driver: _DriverProbe, connection: _ConnectionProbe, real: Any) -> None:
        self.driver = driver
        self.connection = connection
        self.real = real
        self.prepare_count = 0
        self.execute_count = 0
        self.close_count = 0
        self.bound_devices: list[int] = []

    def prepare(self, sql: str) -> None:
        self.real.prepare(sql)
        self.prepare_count += 1

    def bind(self, params: list[object]) -> None:
        self.bound_devices.append(len(params))
        self.real.bind(params)

    def execute(self) -> int:
        hook = self.driver.execute_hook
        if hook is not None:
            return hook(self)
        result = self.real.execute()
        self.execute_count += 1
        return result

    def close(self) -> None:
        if self.close_count == 0:
            self.real.close()
        self.close_count += 1


class _ConnectionProbe:
    def __init__(self, driver: _DriverProbe, real: Any) -> None:
        self.driver = driver
        self.real = real
        self.close_count = 0

    def query(self, sql: str) -> Any:
        return self.real.query(sql)

    def execute(self, sql: str) -> int:
        return int(self.real.execute(sql))

    def stmt2_statement(self) -> _StatementProbe:
        statement = _StatementProbe(self.driver, self, self.real.stmt2_statement())
        self.driver.statements.append(statement)
        return statement

    def close(self) -> None:
        if self.close_count == 0:
            self.real.close()
        self.close_count += 1


class _DriverProbe:
    """保留 taosws 全部真实列绑定和 I/O，仅观察连接及 statement 生命周期。"""

    def __init__(self, real: Any) -> None:
        self.real = real
        self.connections: list[_ConnectionProbe] = []
        self.statements: list[_StatementProbe] = []
        self.execute_hook: Callable[[_StatementProbe], int] | None = None

    def connect(self, **kwargs: object) -> _ConnectionProbe:
        connection = _ConnectionProbe(self, self.real.connect(**kwargs))
        self.connections.append(connection)
        return connection

    def __getattr__(self, name: str) -> Any:
        return getattr(self.real, name)


class _Resolver:
    def __init__(self, schemas: Mapping[str, WideSchema]) -> None:
        self._schemas = schemas

    async def resolve_wide_schema(
        self, record: WideIdentity, *, type_mapping: object, max_row_bytes: int
    ) -> WideSchema:
        return self._schemas[record.device_config_id]

    def wide_device_type(self, identity: WideIdentity) -> str:
        return DEVICE_TYPE


class _Evidence:
    def __init__(self) -> None:
        self.blocks: list[tuple[str, int, int, str, bytes]] = []

    def block_partition(
        self, topic: str, partition: int, offset: int, reason: str, payload: bytes
    ) -> None:
        self.blocks.append((topic, partition, offset, reason, payload))


class _ObservedOffsets(PartitionOffsets):
    def __init__(self) -> None:
        super().__init__(partition=7, generation=3, next_commit_offset=0)
        self.confirmations: list[int] = []

    def confirm(self, *, generation: int, offset: int) -> int:
        result = super().confirm(generation=generation, offset=offset)
        self.confirmations.append(offset)
        return result


@dataclass
class _Environment:
    stack: R5Stack
    driver: _DriverProbe
    config: StorageConfig
    metadata: MetadataStore
    backend: TDengineBackend
    service: WideIngestionService
    evidence: _Evidence
    metrics: StorageRuntimeMetrics


@pytest.fixture(scope="module")
def stack() -> Iterator[R5Stack]:
    assert docker_available(), "真实多设备集成测试要求 Docker daemon"
    value = up_stack()
    try:
        assert value.tdengine_sql("SELECT SERVER_VERSION()")["data"][0][0] == "3.3.6.13"
        yield value
    finally:
        down_stack(value)


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> _DriverProbe:
    real_import = import_module
    probe = _DriverProbe(real_import("taosws"))
    monkeypatch.setattr(
        "storage_service.infrastructure.adapters.outbound.tdengine_backend.import_module",
        lambda name: probe if name == "taosws" else real_import(name),
    )
    monkeypatch.setenv("WIND_HUB_TDENGINE_USER", "root")
    monkeypatch.setenv("WIND_HUB_TDENGINE_PASSWORD", "taosdata")
    return probe


def _schema(
    suffix: str,
    name: str,
    *,
    level: WideDataLevel = WideDataLevel.PROTOCOL,
) -> WideSchema:
    return WideSchema.build(
        point_table_id=f"pt_{name}_{suffix}",
        point_table_version="1.0.0",
        data_level=level,
        points=tuple(PhysicalPoint(f"p{index:03d}", "DOUBLE") for index in range(POINT_COUNT)),
        max_row_bytes=65536,
    )


def _config_id(schema: WideSchema) -> str:
    return f"device-{schema.point_table_id}-v1.0.0"


def _sample_time(timestamp_us: int) -> str:
    return (
        datetime.fromtimestamp(timestamp_us / 1_000_000, tz=UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _expected_value(base: int, index: int, quality_shift: int) -> float | None:
    quality = (index + quality_shift) % 4
    return None if quality == int(PointQuality.NOT_COLLECTED) else base + index + 0.25


def _record(
    schema: WideSchema,
    *,
    farm: str,
    device: str,
    offset: int,
    timestamp_us: int,
    base: int,
    quality_shift: int = 0,
) -> KafkaRecord:
    points: dict[str, list[float | int | None]] = {}
    for index, column in enumerate(schema.columns):
        quality = (index + quality_shift) % 4
        points[column.point_id] = [_expected_value(base, index, quality_shift), quality]
    payload = {
        "schema_version": 3,
        "wind_farm_id": farm,
        "device_config_id": _config_id(schema),
        "device_id": device,
        "data_level": schema.data_level.value,
        "sample_time": _sample_time(timestamp_us),
        "points": points,
    }
    return KafkaRecord(
        topic="history",
        partition=7,
        offset=offset,
        key=f"{farm}:{device}".encode(),
        value=json.dumps(payload, separators=(",", ":")).encode(),
    )


@asynccontextmanager
async def _environment(
    stack: R5Stack,
    driver: _DriverProbe,
    tmp_path: Path,
    schemas: Sequence[WideSchema],
    farms: Sequence[str],
    *,
    connections: int = 1,
    write_timeout_ms: int = 10_000,
) -> AsyncIterator[_Environment, None]:
    base = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )
    ingestion = base.tdengine.ingestion.model_copy(
        update={"writer_count": connections, "write_timeout_ms": write_timeout_ms}
    )
    tdengine = base.tdengine.model_copy(
        update={
            "endpoint": stack.tdengine_rest,
            "max_connections": connections,
            "max_inflight_sql": connections,
            "ingestion": ingestion,
        }
    )
    config = base.model_copy(update={"tdengine": tdengine, "allowed_wind_farms": list(farms)})
    metadata = MetadataStore(tmp_path / f"metadata-{uuid.uuid4().hex}.sqlite3")
    metadata.open()
    metadata.register_semantic_pins(
        point_table_versions={
            schema.point_table_id: (f"semantic-{schema.point_table_id}", "closure-1.0.0")
            for schema in schemas
        },
        business_points_pin=None,
    )
    backend = TDengineBackend(config=config, metadata=metadata)
    evidence = _Evidence()
    metrics = StorageRuntimeMetrics()
    service = WideIngestionService(
        storage=backend,
        resolver=_Resolver({_config_id(schema): schema for schema in schemas}),
        evidence=evidence,
        metrics=metrics,
        allowed_wind_farms=frozenset(farms),
        type_mapping=config.tdengine.type_mapping,
        max_batch_rows=1000,
        max_batch_bytes=8_388_608,
        writer_count=connections,
    )
    databases = {schema.database_name(farm) for schema in schemas for farm in farms}
    try:
        await backend.open()
        yield _Environment(stack, driver, config, metadata, backend, service, evidence, metrics)
    finally:
        try:
            await backend.close()
        finally:
            try:
                metadata.close()
            finally:
                for database in databases:
                    stack.tdengine_sql(f"DROP DATABASE IF EXISTS {database}", timeout=30)


async def _assert_full_row(
    environment: _Environment,
    schema: WideSchema,
    *,
    farm: str,
    device: str,
    timestamp_us: int,
    base: int,
    quality_shift: int = 0,
) -> None:
    rows = await environment.backend.fetch_wide_history(
        schema,
        WideHistoryFilter(
            wind_farm_id=farm,
            device_id=device,
            device_config_id=_config_id(schema),
            data_level=schema.data_level,
            start_time_us=timestamp_us - 1,
            end_time_us=timestamp_us + 1,
            point_ids=tuple(column.point_id for column in schema.columns),
            limit=10,
        ),
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.sample_time_us == timestamp_us
    assert len(row.points) == POINT_COUNT
    for index, (point_id, value, quality) in enumerate(row.points):
        assert point_id == schema.columns[index].point_id
        assert value == _expected_value(base, index, quality_shift)
        assert quality == PointQuality((index + quality_shift) % 4)


def _assert_tags(environment: _Environment, schema: WideSchema, *, farm: str, device: str) -> None:
    database = schema.database_name(farm)
    table = schema.subtable_name(device)
    rows = environment.stack.tdengine_sql(f"SHOW TAGS FROM {database}.{table}")["data"]
    expected = (
        ("device_id", "VARCHAR(128)", device),
        ("device_type", "VARCHAR(64)", DEVICE_TYPE),
        ("point_table_id", "VARCHAR(192)", schema.point_table_id),
        (
            "data_level",
            "TINYINT",
            str(
                {
                    WideDataLevel.RAW: 1,
                    WideDataLevel.PROTOCOL: 2,
                    WideDataLevel.BUSINESS: 3,
                }[schema.data_level]
            ),
        ),
        ("schema_fingerprint", "VARCHAR(64)", schema.fingerprint),
    )
    assert len(rows) == 5
    for row, (name, sql_type, value) in zip(rows, expected, strict=True):
        assert (str(row[3]), str(row[4]).upper(), str(row[5])) == (name, sql_type, value)
        assert (row[0], row[1], row[2]) == (table, database, schema.supertable_name())


def _family(schema: WideSchema, farm: str) -> str:
    return f"{schema.database_name(farm)}.{schema.supertable_name()}"


async def _assert_unknown_fenced(
    environment: _Environment,
    schema: WideSchema,
    *,
    farm: str,
    devices: Sequence[str],
    next_timestamp_us: int,
) -> None:
    database = schema.database_name(farm)
    assert environment.backend._wide_unknown_tables == {
        f"{database}.{schema.subtable_name(device)}" for device in devices
    }
    offsets = _ObservedOffsets()
    with pytest.raises(TDengineSchemaConflictError, match="UNKNOWN|unresolved"):
        await environment.service.process(
            generation=offsets.generation,
            offsets=offsets,
            records=[
                _record(
                    schema,
                    farm=farm,
                    device=devices[0],
                    offset=0,
                    timestamp_us=next_timestamp_us,
                    base=90_000,
                )
            ],
        )
    assert offsets.next_commit_offset == 0
    assert offsets.confirmations == []


async def test_same_family_multi_device_single_execute_with_complete_rows_and_tags(
    stack: R5Stack, driver: _DriverProbe, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex
    schema = _schema(suffix, "single_execute")
    farm = f"farm-a-{suffix}"
    devices = ("wtg001", "wtg002")
    async with _environment(stack, driver, tmp_path, [schema], [farm]) as environment:
        offsets = _ObservedOffsets()
        result = await environment.service.process(
            generation=offsets.generation,
            offsets=offsets,
            records=[
                _record(
                    schema,
                    farm=farm,
                    device=device,
                    offset=index,
                    timestamp_us=SAMPLE_TIME_US + index,
                    base=10_000 + index * 1_000,
                )
                for index, device in enumerate(devices)
            ],
        )
        assert result.next_commit_offset == 2
        assert result.accepted_records == 2
        assert offsets.confirmations == [0, 1]
        assert len(driver.statements) == 1
        statement = driver.statements[0]
        assert statement.prepare_count == 1
        assert statement.execute_count == 1
        assert statement.bound_devices == [2]
        for index, device in enumerate(devices):
            _assert_tags(environment, schema, farm=farm, device=device)
            await _assert_full_row(
                environment,
                schema,
                farm=farm,
                device=device,
                timestamp_us=SAMPLE_TIME_US + index,
                base=10_000 + index * 1_000,
            )


async def test_farm_schema_and_data_level_never_share_one_execute(
    stack: R5Stack, driver: _DriverProbe, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex
    protocol = _schema(suffix, "protocol")
    other_schema = _schema(suffix, "other_schema")
    raw = _schema(suffix, "raw", level=WideDataLevel.RAW)
    farms = (f"farm-a-{suffix}", f"farm-b-{suffix}")
    async with _environment(
        stack, driver, tmp_path, [protocol, other_schema, raw], farms, connections=2
    ) as environment:
        cases = (
            (protocol, farms[0], "wtg001", 11_000),
            (protocol, farms[0], "wtg002", 12_000),
            (protocol, farms[1], "wtg003", 13_000),
            (other_schema, farms[0], "wtg004", 14_000),
            (raw, farms[0], "wtg005", 15_000),
        )
        offsets = _ObservedOffsets()
        result = await environment.service.process(
            generation=offsets.generation,
            offsets=offsets,
            records=[
                _record(
                    schema,
                    farm=farm,
                    device=device,
                    offset=offset,
                    timestamp_us=SAMPLE_TIME_US + offset,
                    base=base,
                )
                for offset, (schema, farm, device, base) in enumerate(cases)
            ],
        )
        assert result.next_commit_offset == len(cases)
        assert len(driver.statements) == 4
        assert sum(statement.execute_count for statement in driver.statements) == 4
        assert sorted(
            devices for statement in driver.statements for devices in statement.bound_devices
        ) == [1, 1, 1, 2]
        assert len({_family(schema, farm) for schema, farm, _device, _base in cases}) == 4
        for offset, (schema, farm, device, base) in enumerate(cases):
            _assert_tags(environment, schema, farm=farm, device=device)
            await _assert_full_row(
                environment,
                schema,
                farm=farm,
                device=device,
                timestamp_us=SAMPLE_TIME_US + offset,
                base=base,
            )


async def test_same_device_timestamp_last_kafka_offset_overwrites_complete_row(
    stack: R5Stack, driver: _DriverProbe, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex
    schema = _schema(suffix, "last_offset_wins")
    farm = f"farm-{suffix}"
    async with _environment(stack, driver, tmp_path, [schema], [farm]) as environment:
        offsets = _ObservedOffsets()
        result = await environment.service.process(
            generation=offsets.generation,
            offsets=offsets,
            records=[
                _record(
                    schema,
                    farm=farm,
                    device="wtg001",
                    offset=0,
                    timestamp_us=SAMPLE_TIME_US,
                    base=10_000,
                ),
                _record(
                    schema,
                    farm=farm,
                    device="wtg001",
                    offset=1,
                    timestamp_us=SAMPLE_TIME_US,
                    base=20_000,
                    quality_shift=1,
                ),
            ],
        )
        assert result.accepted_records == 2
        assert result.next_commit_offset == 2
        assert len(driver.statements) == 1
        assert driver.statements[0].execute_count == 1
        await _assert_full_row(
            environment,
            schema,
            farm=farm,
            device="wtg001",
            timestamp_us=SAMPLE_TIME_US,
            base=20_000,
            quality_shift=1,
        )


async def test_statement_reuse_is_per_connection_and_schema(
    stack: R5Stack, driver: _DriverProbe, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex
    first = _schema(suffix, "cache_first")
    second = _schema(suffix, "cache_second")
    farm = f"farm-{suffix}"
    async with _environment(stack, driver, tmp_path, [first, second], [farm]) as environment:
        for cycle in range(2):
            for schema_index, schema in enumerate((first, second)):
                offsets = _ObservedOffsets()
                await environment.service.process(
                    generation=offsets.generation,
                    offsets=offsets,
                    records=[
                        _record(
                            schema,
                            farm=farm,
                            device=f"wtg{schema_index:03d}",
                            offset=0,
                            timestamp_us=SAMPLE_TIME_US + cycle,
                            base=10_000 + cycle,
                        )
                    ],
                )
        assert len(driver.connections) == 1
        assert len(driver.statements) == 2
        assert [statement.prepare_count for statement in driver.statements] == [1, 1]
        assert [statement.execute_count for statement in driver.statements] == [2, 2]
        cache = environment.backend._wide_statements[id(driver.connections[0])]
        assert set(cache) == {_family(first, farm), _family(second, farm)}
        assert len({id(statement) for statement in cache.values()}) == 2


async def test_statement_lru_eviction_closes_only_least_recently_used(
    stack: R5Stack, driver: _DriverProbe, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex
    schemas = [_schema(suffix, f"lru_{index:02d}") for index in range(CACHE_LIMIT + 1)]
    farm = f"farm-{suffix}"
    async with _environment(stack, driver, tmp_path, schemas, [farm]) as environment:
        for index, schema in enumerate(schemas[:CACHE_LIMIT]):
            offsets = _ObservedOffsets()
            await environment.service.process(
                generation=offsets.generation,
                offsets=offsets,
                records=[
                    _record(
                        schema,
                        farm=farm,
                        device="wtg001",
                        offset=0,
                        timestamp_us=SAMPLE_TIME_US,
                        base=index * 1_000,
                    )
                ],
            )
        connection_id = id(driver.connections[0])
        cache = environment.backend._wide_statements[connection_id]
        assert len(cache) == CACHE_LIMIT
        reused = driver.statements[0]
        evicted = driver.statements[1]
        offsets = _ObservedOffsets()
        await environment.service.process(
            generation=offsets.generation,
            offsets=offsets,
            records=[
                _record(
                    schemas[0],
                    farm=farm,
                    device="wtg001",
                    offset=0,
                    timestamp_us=SAMPLE_TIME_US + 1,
                    base=80_000,
                )
            ],
        )
        assert reused.close_count == 0
        assert evicted.close_count == 0
        offsets = _ObservedOffsets()
        await environment.service.process(
            generation=offsets.generation,
            offsets=offsets,
            records=[
                _record(
                    schemas[-1],
                    farm=farm,
                    device="wtg001",
                    offset=0,
                    timestamp_us=SAMPLE_TIME_US,
                    base=90_000,
                )
            ],
        )
        cache = environment.backend._wide_statements[connection_id]
        assert len(cache) == CACHE_LIMIT
        assert _family(schemas[0], farm) in cache
        assert _family(schemas[1], farm) not in cache
        assert _family(schemas[-1], farm) in cache
        assert reused.close_count == 0
        assert evicted.close_count == 1
        assert sum(statement.close_count for statement in driver.statements) == 1
    assert all(statement.close_count == 1 for statement in driver.statements)
    assert all(connection.close_count == 1 for connection in driver.connections)


async def test_concurrent_connections_never_share_cached_statement(
    stack: R5Stack, driver: _DriverProbe, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex
    schema = _schema(suffix, "two_connections")
    farm = f"farm-{suffix}"
    entered = 0
    entered_lock = threading.Lock()
    both_entered = threading.Event()
    release = threading.Event()

    def synchronized_execute(statement: _StatementProbe) -> int:
        nonlocal entered
        with entered_lock:
            entered += 1
            if entered == 2:
                both_entered.set()
        assert both_entered.wait(timeout=10)
        assert release.wait(timeout=10)
        result = statement.real.execute()
        statement.execute_count += 1
        return result

    async with _environment(
        stack, driver, tmp_path, [schema], [farm], connections=2
    ) as environment:
        for device in ("wtg001", "wtg002"):
            await environment.backend.ensure_wide_table(
                schema, wind_farm_id=farm, device_id=device, device_type=DEVICE_TYPE
            )
        driver.execute_hook = synchronized_execute
        rows = [
            WideRecord.from_kafka_json(
                _record(
                    schema,
                    farm=farm,
                    device=device,
                    offset=index,
                    timestamp_us=SAMPLE_TIME_US + index,
                    base=10_000 + index,
                ).value,
                physical_points=tuple(column.point_id for column in schema.columns),
            )
            for index, device in enumerate(("wtg001", "wtg002"))
        ]
        writes = [
            asyncio.create_task(environment.backend.insert_wide_batch(schema, [row]))
            for row in rows
        ]
        task = asyncio.gather(*writes)
        try:
            assert await asyncio.to_thread(both_entered.wait, 10)
        finally:
            release.set()
        await asyncio.wait_for(task, timeout=10)
        driver.execute_hook = None
        assert len(driver.connections) == 2
        assert len(driver.statements) == 2
        assert len({id(statement.connection) for statement in driver.statements}) == 2
        assert len({id(statement.real) for statement in driver.statements}) == 2
        family = _family(schema, farm)
        assert {
            connection_id: tuple(cache)
            for connection_id, cache in environment.backend._wide_statements.items()
        } == {
            id(driver.statements[0].connection): (family,),
            id(driver.statements[1].connection): (family,),
        }


async def test_execute_timeout_fences_all_tables_without_offset_and_cleans_connection(
    stack: R5Stack, driver: _DriverProbe, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex
    schema = _schema(suffix, "execute_timeout")
    farm = f"farm-{suffix}"
    started = threading.Event()
    release = threading.Event()

    def late_execute(statement: _StatementProbe) -> int:
        started.set()
        assert release.wait(timeout=10)
        result = statement.real.execute()
        statement.execute_count += 1
        return result

    async with _environment(stack, driver, tmp_path, [schema], [farm]) as environment:
        devices = ("wtg001", "wtg002")
        for device in devices:
            await environment.backend.ensure_wide_table(
                schema, wind_farm_id=farm, device_id=device, device_type=DEVICE_TYPE
            )
        original_config = environment.backend._config
        ingestion = original_config.tdengine.ingestion.model_copy(update={"write_timeout_ms": 100})
        environment.backend._config = original_config.model_copy(
            update={
                "tdengine": original_config.tdengine.model_copy(update={"ingestion": ingestion})
            }
        )
        driver.execute_hook = late_execute
        offsets = _ObservedOffsets()
        try:
            with pytest.raises(IngestionBlockedError):
                await environment.service.process(
                    generation=offsets.generation,
                    offsets=offsets,
                    records=[
                        _record(
                            schema,
                            farm=farm,
                            device=device,
                            offset=index,
                            timestamp_us=SAMPLE_TIME_US + index,
                            base=10_000 + index,
                        )
                        for index, device in enumerate(devices)
                    ],
                )
            assert started.is_set()
            assert offsets.next_commit_offset == 0
            assert offsets.confirmations == []
            assert environment.metrics.snapshot().write_unknown == 1
            assert environment.evidence.blocks[0][3] == "TDENGINE_WIDE_IO_UNKNOWN: TimeoutError"
            await _assert_unknown_fenced(
                environment,
                schema,
                farm=farm,
                devices=devices,
                next_timestamp_us=SAMPLE_TIME_US + 100,
            )
        finally:
            release.set()
            driver.execute_hook = None
            environment.backend._config = original_config
        for _ in range(100):
            if driver.connections[0].close_count:
                break
            await asyncio.sleep(0.02)
        assert driver.connections[0].close_count == 1
        assert all(statement.close_count == 1 for statement in driver.statements)
        assert id(driver.connections[0]) not in environment.backend._wide_statements
        driver.execute_hook = None


async def test_partial_success_after_real_execute_is_unknown_and_not_committed(
    stack: R5Stack, driver: _DriverProbe, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex
    schema = _schema(suffix, "partial_success")
    farm = f"farm-{suffix}"

    def persist_then_fail(statement: _StatementProbe) -> int:
        statement.real.execute()
        statement.execute_count += 1
        raise OSError("injected post-execute partial-success uncertainty")

    async with _environment(stack, driver, tmp_path, [schema], [farm]) as environment:
        driver.execute_hook = persist_then_fail
        offsets = _ObservedOffsets()
        devices = ("wtg001", "wtg002")
        with pytest.raises(IngestionBlockedError):
            await environment.service.process(
                generation=offsets.generation,
                offsets=offsets,
                records=[
                    _record(
                        schema,
                        farm=farm,
                        device=device,
                        offset=index,
                        timestamp_us=SAMPLE_TIME_US + index,
                        base=10_000 + index,
                    )
                    for index, device in enumerate(devices)
                ],
            )
        assert offsets.next_commit_offset == 0
        assert offsets.confirmations == []
        assert environment.metrics.snapshot().write_unknown == 1
        assert environment.evidence.blocks[0][3] == "TDENGINE_WIDE_IO_UNKNOWN: OSError"
        for device in devices:
            rows = stack.tdengine_sql(
                f"SELECT COUNT(*) FROM {schema.database_name(farm)}.{schema.subtable_name(device)}"
            )["data"]
            assert rows[0][0] == 1
        await _assert_unknown_fenced(
            environment,
            schema,
            farm=farm,
            devices=devices,
            next_timestamp_us=SAMPLE_TIME_US + 100,
        )
        driver.execute_hook = None
    assert all(statement.close_count == 1 for statement in driver.statements)
    assert all(connection.close_count == 1 for connection in driver.connections)


async def test_real_connection_loss_discards_cache_and_fences_without_offset(
    stack: R5Stack, driver: _DriverProbe, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex
    schema = _schema(suffix, "connection_loss")
    farm = f"farm-{suffix}"

    def disconnect(statement: _StatementProbe) -> int:
        statement.connection.close()
        raise OSError("injected real WebSocket connection loss")

    async with _environment(stack, driver, tmp_path, [schema], [farm]) as environment:
        driver.execute_hook = disconnect
        offsets = _ObservedOffsets()
        with pytest.raises(IngestionBlockedError):
            await environment.service.process(
                generation=offsets.generation,
                offsets=offsets,
                records=[
                    _record(
                        schema,
                        farm=farm,
                        device="wtg001",
                        offset=0,
                        timestamp_us=SAMPLE_TIME_US,
                        base=10_000,
                    )
                ],
            )
        assert offsets.next_commit_offset == 0
        assert offsets.confirmations == []
        assert environment.metrics.snapshot().write_unknown == 1
        assert environment.evidence.blocks[0][3] == "TDENGINE_WIDE_IO_UNKNOWN: OSError"
        assert driver.connections[0].close_count >= 1
        assert all(statement.close_count == 1 for statement in driver.statements)
        assert environment.backend._wide_statements == {}
        await _assert_unknown_fenced(
            environment,
            schema,
            farm=farm,
            devices=("wtg001",),
            next_timestamp_us=SAMPLE_TIME_US + 100,
        )
        driver.execute_hook = None

        # 新设备走替换连接和独立 statement，旧 UNKNOWN 不跨连接清除。
        recovery_offsets = _ObservedOffsets()
        result = await environment.service.process(
            generation=recovery_offsets.generation,
            offsets=recovery_offsets,
            records=[
                _record(
                    schema,
                    farm=farm,
                    device="wtg002",
                    offset=0,
                    timestamp_us=SAMPLE_TIME_US,
                    base=20_000,
                )
            ],
        )
        assert result.next_commit_offset == 1
        assert len(driver.connections) == 2
        assert driver.connections[1] is not driver.connections[0]
        assert driver.statements[-1].connection is driver.connections[1]
        assert driver.statements[-1].real is not driver.statements[0].real


async def test_repeated_open_close_releases_connections_statements_and_threads(
    stack: R5Stack, driver: _DriverProbe, tmp_path: Path
) -> None:
    suffix = uuid.uuid4().hex
    schema = _schema(suffix, "repeated_lifecycle")
    farm = f"farm-{suffix}"
    baseline = {thread.ident for thread in threading.enumerate()}
    for cycle in range(3):
        async with _environment(stack, driver, tmp_path, [schema], [farm]) as environment:
            offsets = _ObservedOffsets()
            result = await environment.service.process(
                generation=offsets.generation,
                offsets=offsets,
                records=[
                    _record(
                        schema,
                        farm=farm,
                        device=f"wtg{cycle:03d}",
                        offset=0,
                        timestamp_us=SAMPLE_TIME_US + cycle,
                        base=10_000 + cycle,
                    )
                ],
            )
            assert result.next_commit_offset == 1
            await environment.backend.close()
            assert environment.backend._pool_size == 0
            assert environment.backend._wide_statements == {}
            assert environment.backend._wide_current_database == {}
    assert Counter(statement.close_count for statement in driver.statements) == {1: 3}
    assert Counter(connection.close_count for connection in driver.connections) == {1: 3}
    for _ in range(100):
        current = {
            thread.ident
            for thread in threading.enumerate()
            if thread.name.startswith("storage-tdengine")
        }
        if not current - baseline:
            break
        await asyncio.sleep(0.02)
    assert not current - baseline
