"""Wide RPC serialization must reject values outside the physical TDengine column."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as pb
from storage_service.application.wide_query import WideHistoryPage, WideHistoryRow
from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_record import WideDataLevel
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.inbound.grpc_server import StorageServicer
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineSchemaConflictError,
)


async def _read(sql_type: str, value: object) -> pb.QueryWideRowsResponse:
    service = object.__new__(StorageServicer)
    service._auth = Mock()
    service._consumer = SimpleNamespace(recovering=False)
    service._config = SimpleNamespace(tdengine=SimpleNamespace(type_mapping=Mock()))
    schema = WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("value", sql_type),),
        max_row_bytes=65536,
    )
    service._point_tables = Mock()
    service._point_tables.resolve_wide_schema = AsyncMock(return_value=schema)
    service._point_tables.wide_device_type.return_value = "turbine"
    service._point_tables.get_wide_unit = AsyncMock(return_value="")
    service._wide_query = Mock()
    service._wide_query.query = AsyncMock(
        return_value=WideHistoryPage(
            rows=(WideHistoryRow(1_795_608_000_000_000, (
                ("value", value, PointQuality.GOOD),
            )),),
            next_page_token="",
            complete=True,
            schema_fingerprint=schema.fingerprint,
        )
    )
    request = pb.QueryWideRowsRequest(
        wind_farm_id="farm",
        device_id="wtg1",
        device_config_id="device_farm_v1.0.0",
        data_level=pb.STORAGE_DATA_LEVEL_PROTOCOL,
        start_time=Timestamp(seconds=1795607900),
        end_time=Timestamp(seconds=1795608100),
        point_ids=("value",),
        page_size=10,
    )
    return await StorageServicer.QueryWideRows.__wrapped__(service, request, Mock())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql_type", "value"),
    [
        ("TINYINT", 128),
        ("TINYINT", -129),
        ("SMALLINT", 32768),
        ("INT", -(2**31) - 1),
        ("BIGINT", 2**63),
        ("TINYINT UNSIGNED", -1),
        ("SMALLINT UNSIGNED", 2**16),
        ("INT UNSIGNED", 2**32),
        ("BIGINT UNSIGNED", 2**64),
        ("INT", True),
        ("FLOAT", float("nan")),
        ("FLOAT", float("inf")),
        ("FLOAT", 1e100),
        ("DOUBLE", float("-inf")),
        ("DOUBLE", 10**500),
        ("DOUBLE", 2**53 + 1),
        ("VARCHAR(4)", "你好"),
        ("VARBINARY(3)", b"abcd"),
    ],
)
async def test_wide_rpc_rejects_out_of_range_physical_values(
    sql_type: str, value: object,
) -> None:
    with pytest.raises(TDengineSchemaConflictError):
        await _read(sql_type, value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql_type", "value", "field"),
    [
        ("TINYINT", -128, "signed_value"),
        ("TINYINT", 127, "signed_value"),
        ("BIGINT", -(2**63), "signed_value"),
        ("BIGINT UNSIGNED", 2**64 - 1, "unsigned_value"),
        ("FLOAT", 1.25, "float_value"),
        ("VARCHAR(6)", "你好", "string_value"),
        ("VARBINARY(3)", b"abc", "bytes_value"),
    ],
)
async def test_wide_rpc_preserves_valid_boundary_values(
    sql_type: str, value: object, field: str,
) -> None:
    result = await _read(sql_type, value)
    point = result.rows[0].points[0]
    assert point.WhichOneof("value") == field
    assert getattr(point, field) == value
