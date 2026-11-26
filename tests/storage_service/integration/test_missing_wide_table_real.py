"""真实 TDengine 宽表缺表、登记冲突与 replay 可见性集成测试。"""

from __future__ import annotations

import sqlite3
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from storage_service.application.config import StorageConfig, load_storage_config
from storage_service.application.wide_aggregate import WideAggregationService
from storage_service.application.wide_group_query import (
    GroupedWideRows,
    GroupedWideSelector,
    WideGroupedHistoryService,
)
from storage_service.application.wide_query import (
    WideCursorCodec,
    WideHistoryFilter,
    WideHistoryQueryService,
)
from storage_service.domain.errors import HistoryReplayUnavailableError, TDengineSchemaConflictError
from storage_service.domain.quality_bitmap import PointQuality, pack_quality_bitmap
from storage_service.domain.wide_record import WideDataLevel, WideIdentity, WideRecord
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from storage_service.infrastructure.adapters.outbound.tdengine_backend import TDengineBackend
from tests.fixtures.services.r5_stack import (
    R5Stack,
    docker_available,
    down_stack,
    up_stack,
)

ROOT = Path(__file__).resolve().parents[3]
DEVICE_TYPE = "wind_turbine"
POINT_ID = "active_power"
pytestmark = [pytest.mark.integration, pytest.mark.real_service, pytest.mark.docker]


@dataclass
class RealWideEnvironment:
    stack: R5Stack
    config: StorageConfig
    metadata: MetadataStore
    backend: TDengineBackend
    history: WideHistoryQueryService
    schema: WideSchema
    farm: str
    metadata_path: Path
    start_us: int
    end_us: int

    @property
    def database(self) -> str:
        return self.schema.database_name(self.farm)

    def physical_name(self, device_id: str) -> str:
        return f"{self.database}.{self.schema.subtable_name(device_id)}"


@pytest.fixture(scope="module")
def stack() -> Iterator[R5Stack]:
    assert docker_available(), "真实宽表集成测试要求 Docker daemon"
    value = up_stack()
    try:
        version = value.tdengine_sql("SELECT SERVER_VERSION()")
        assert version["data"][0][0] == "3.3.6.13"
        yield value
    finally:
        down_stack(value)


@pytest.fixture
async def real_wide(
    stack: R5Stack, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[RealWideEnvironment, None]:
    suffix = uuid.uuid4().hex
    farm = f"d5-missing-{suffix}"
    table_id = f"point_table_d5_{suffix}_v1.0.0"
    schema = WideSchema.build(
        point_table_id=table_id,
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint(POINT_ID, "DOUBLE"),),
        max_row_bytes=65536,
    )
    base = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v2.0.0.yaml").read_bytes()
    )
    tdengine = base.tdengine.model_copy(
        update={
            "endpoint": stack.tdengine_rest,
            "max_connections": 2,
            "max_inflight_sql": 2,
        }
    )
    config = base.model_copy(update={"tdengine": tdengine, "allowed_wind_farms": [farm]})
    monkeypatch.setenv("WIND_HUB_TDENGINE_USER", "root")
    monkeypatch.setenv("WIND_HUB_TDENGINE_PASSWORD", "taosdata")
    metadata_path = tmp_path / "metadata.sqlite3"
    metadata = MetadataStore(metadata_path)
    metadata.open()
    metadata.register_semantic_pins(
        point_table_versions={table_id: ("d5-point-table", "d5-point-table-1.0.0")},
        business_points_pin=None,
    )
    backend = TDengineBackend(config=config, metadata=metadata)
    now_us = time.time_ns() // 1000
    environment = RealWideEnvironment(
        stack=stack,
        config=config,
        metadata=metadata,
        backend=backend,
        history=WideHistoryQueryService(
            backend=backend,
            codec=WideCursorCodec(b"d5-missing-wide-table-secret-32bytes"),
        ),
        schema=schema,
        farm=farm,
        metadata_path=metadata_path,
        start_us=now_us - 1_000_000,
        end_us=now_us + 10_000_000,
    )
    await backend.open()
    try:
        yield environment
    finally:
        try:
            await backend.close()
        finally:
            try:
                metadata.close()
            finally:
                stack.tdengine_sql(f"DROP DATABASE IF EXISTS {environment.database}")


def _filter(
    environment: RealWideEnvironment, device_id: str, *, quality: bool
) -> WideHistoryFilter:
    return WideHistoryFilter(
        wind_farm_id=environment.farm,
        device_id=device_id,
        device_config_id=f"device-{device_id}-v1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        start_time_us=environment.start_us,
        end_time_us=environment.end_us,
        point_ids=(POINT_ID,),
        limit=10,
        qualities=(int(PointQuality.GOOD),) if quality else (),
    )


async def _create_table(environment: RealWideEnvironment, device_id: str) -> None:
    await environment.backend.ensure_wide_table(
        environment.schema,
        wind_farm_id=environment.farm,
        device_id=device_id,
        device_type=DEVICE_TYPE,
    )


def _force_cold_verification(environment: RealWideEnvironment, device_id: str) -> None:
    # 直接篡改持久登记或 TAG 后，清除该目标的进程内验证缓存以模拟冷启动读取。
    environment.backend._verified_tables.discard(environment.physical_name(device_id))


async def _assert_conflict(environment: RealWideEnvironment, device_id: str, quality: bool) -> None:
    with pytest.raises(TDengineSchemaConflictError):
        await environment.history.query(
            schema=environment.schema,
            filter_=_filter(environment, device_id, quality=quality),
            device_type=DEVICE_TYPE,
        )


@pytest.mark.parametrize("quality", [False, True], ids=["plain", "quality"])
async def test_registered_device_without_subtable_returns_empty(
    real_wide: RealWideEnvironment, quality: bool
) -> None:
    page = await real_wide.history.query(
        schema=real_wide.schema,
        filter_=_filter(real_wide, "wtg-never-created", quality=quality),
        device_type=DEVICE_TYPE,
    )
    assert page.complete
    assert page.rows == ()
    assert page.next_page_token == ""


class _TwoDeviceResolver:
    def __init__(self, environment: RealWideEnvironment, device_ids: tuple[str, ...]) -> None:
        self._environment = environment
        self._device_ids = device_ids

    def list_wide_identities(
        self,
        *,
        wind_farm_id: str,
        device_ids: tuple[str, ...],
        point_table_id: str,
        device_type: str,
        data_level: WideDataLevel,
    ) -> tuple[WideIdentity, ...]:
        assert wind_farm_id == self._environment.farm
        assert device_ids == self._device_ids
        assert point_table_id == self._environment.schema.point_table_id
        assert device_type == DEVICE_TYPE
        assert data_level == WideDataLevel.PROTOCOL
        return tuple(
            WideIdentity(wind_farm_id, f"device-{device}-v1.0.0", device, data_level)
            for device in device_ids
        )

    async def resolve_wide_schema(
        self, record: WideIdentity, *, type_mapping: object, max_row_bytes: int
    ) -> WideSchema:
        assert record.device_id in self._device_ids
        assert type_mapping == self._environment.config.tdengine.type_mapping
        assert max_row_bytes == 65536
        return self._environment.schema

    def wide_device_type(self, identity: WideIdentity) -> str:
        assert identity.device_id in self._device_ids
        return DEVICE_TYPE


@pytest.mark.parametrize("quality", [False, True], ids=["plain", "quality"])
async def test_grouped_query_skips_missing_device_and_returns_only_real_rows(
    real_wide: RealWideEnvironment, quality: bool
) -> None:
    created = "wtg-created"
    missing = "wtg-missing"
    devices = (created, missing)
    await _create_table(real_wide, created)
    timestamps = (real_wide.start_us + 100_000, real_wide.start_us + 200_000)
    await real_wide.backend.insert_wide_batch(
        real_wide.schema,
        [
            WideRecord(
                wind_farm_id=real_wide.farm,
                device_config_id=f"device-{created}-v1.0.0",
                device_id=created,
                data_level=WideDataLevel.PROTOCOL,
                sample_time_us=timestamp,
                point_ids=(POINT_ID,),
                values=(100.5 + index,),
                quality_bitmap=pack_quality_bitmap([int(PointQuality.GOOD)]),
            )
            for index, timestamp in enumerate(timestamps)
        ],
    )
    grouped = WideGroupedHistoryService(
        history=real_wide.history,
        aggregation=WideAggregationService(history=real_wide.history, secret=b"g" * 32),
        resolver=_TwoDeviceResolver(real_wide, devices),
        type_mapping=real_wide.config.tdengine.type_mapping,
        cursor_secret=b"c" * 32,
    )
    selector = GroupedWideSelector(
        wind_farm_id=real_wide.farm,
        device_ids=devices,
        device_type=DEVICE_TYPE,
        point_table_id=real_wide.schema.point_table_id,
        data_level=WideDataLevel.PROTOCOL,
        point_ids=(POINT_ID,),
        qualities=(int(PointQuality.GOOD),) if quality else (),
        start_time_us=real_wide.start_us,
        end_time_us=real_wide.end_us,
    )
    groups: list[GroupedWideRows] = []
    token = ""
    for _ in range(4):
        page = await grouped.query(selector=selector, page_size=10, page_token=token)
        groups.extend(page.groups)
        token = page.next_page_token
        if page.complete:
            break
    else:
        pytest.fail("分组宽表查询未在有界页数内完成")
    assert token == ""
    assert len(groups) == 1
    assert groups[0].identity.device_id == created
    assert [row.sample_time_us for row in groups[0].rows] == list(timestamps)
    assert [row.points[0][1] for row in groups[0].rows] == [100.5, 101.5]
    assert all(row.points[0][2] == PointQuality.GOOD for row in groups[0].rows)


@pytest.mark.parametrize("quality", [False, True], ids=["plain", "quality"])
async def test_physical_table_without_registration_is_schema_conflict(
    real_wide: RealWideEnvironment, quality: bool
) -> None:
    device = "wtg-unregistered"
    await _create_table(real_wide, device)
    with sqlite3.connect(real_wide.metadata_path) as connection:
        connection.execute(
            "DELETE FROM storage_objects WHERE name = ?",
            (real_wide.physical_name(device),),
        )
    _force_cold_verification(real_wide, device)
    await _assert_conflict(real_wide, device, quality)


@pytest.mark.parametrize("quality", [False, True], ids=["plain", "quality"])
async def test_creating_registration_is_schema_conflict(
    real_wide: RealWideEnvironment, quality: bool
) -> None:
    device = "wtg-creating"
    await _create_table(real_wide, device)
    with sqlite3.connect(real_wide.metadata_path) as connection:
        connection.execute(
            "UPDATE storage_objects SET state = 'CREATING' WHERE name = ?",
            (real_wide.physical_name(device),),
        )
    _force_cold_verification(real_wide, device)
    await _assert_conflict(real_wide, device, quality)


@pytest.mark.parametrize("quality", [False, True], ids=["plain", "quality"])
async def test_registered_schema_fingerprint_mismatch_is_schema_conflict(
    real_wide: RealWideEnvironment, quality: bool
) -> None:
    device = "wtg-wrong-registration"
    await _create_table(real_wide, device)
    with sqlite3.connect(real_wide.metadata_path) as connection:
        connection.execute(
            "UPDATE storage_objects SET schema_fingerprint = ? WHERE name = ?",
            ("0" * 64, real_wide.physical_name(device)),
        )
    _force_cold_verification(real_wide, device)
    await _assert_conflict(real_wide, device, quality)


@pytest.mark.parametrize("quality", [False, True], ids=["plain", "quality"])
async def test_actual_schema_fingerprint_tag_mismatch_is_schema_conflict(
    real_wide: RealWideEnvironment, quality: bool
) -> None:
    device = "wtg-wrong-tag"
    await _create_table(real_wide, device)
    real_wide.stack.tdengine_sql(
        f"ALTER TABLE {real_wide.physical_name(device)} " f"SET TAG schema_fingerprint='{'0' * 64}'"
    )
    _force_cold_verification(real_wide, device)
    await _assert_conflict(real_wide, device, quality)


@pytest.mark.parametrize("quality", [False, True], ids=["plain", "quality"])
async def test_replay_unavailable_rejects_absent_table_for_plain_and_quality(
    real_wide: RealWideEnvironment, quality: bool
) -> None:
    history = WideHistoryQueryService(
        backend=real_wide.backend,
        codec=WideCursorCodec(b"r" * 32),
        read_available=lambda: False,
    )
    with pytest.raises(HistoryReplayUnavailableError, match="replay"):
        await history.query(
            schema=real_wide.schema,
            filter_=_filter(real_wide, "wtg-absent-during-replay", quality=quality),
            device_type=DEVICE_TYPE,
        )
