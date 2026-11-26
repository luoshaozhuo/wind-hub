"""Grouped RPC serialization must reject values outside the physical TDengine column."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as pb
from storage_service.application.wide_group_query import GroupedWidePage, GroupedWideRows
from storage_service.application.wide_query import WideHistoryRow
from storage_service.domain.quality_bitmap import PointQuality
from storage_service.domain.wide_record import WideDataLevel, WideIdentity
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.inbound.grpc_server import StorageServicer
from storage_service.infrastructure.adapters.outbound.tdengine_backend import (
    TDengineSchemaConflictError,
)


def _schema(sql_type: str) -> WideSchema:
    return WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(PhysicalPoint("value", sql_type),),
        max_row_bytes=65536,
    )


async def _read(
    sql_type: str,
    value: object,
    *,
    wrong_fingerprint: bool = False,
) -> pb.QuerySamplesResponse:
    service = object.__new__(StorageServicer)
    service._auth = Mock()
    service._consumer = SimpleNamespace(recovering=False)
    schema = _schema(sql_type)
    identity = WideIdentity("farm", "device_farm_v1.0.0", "wtg1", WideDataLevel.PROTOCOL)
    service._point_tables = Mock()
    service._point_tables.get_wide_column_metadata = AsyncMock(
        return_value=("value", "business_value", "native", "")
    )
    returned_schema = (
        WideSchema(
            point_table_id=schema.point_table_id,
            point_table_version=schema.point_table_version,
            data_level=schema.data_level,
            columns=schema.columns,
            quality_bytes=schema.quality_bytes,
            declared_row_bytes=schema.declared_row_bytes,
            fingerprint="f" * 64,
        )
        if wrong_fingerprint
        else schema
    )
    service._grouped_query = Mock()
    service._grouped_query.query = AsyncMock(
        return_value=GroupedWidePage(
            groups=(
                GroupedWideRows(
                    identity=identity,
                    device_type="turbine",
                    schema=returned_schema,
                    point_ids=("value",),
                    rows=(
                        WideHistoryRow(
                            1_795_608_000_000_000,
                            (("value", value, PointQuality.GOOD),),
                        ),
                    ),
                ),
            ),
            next_page_token="",
            complete=True,
        )
    )
    request = pb.QuerySamplesRequest(
        filter=pb.StorageFilter(
            wind_farm_id="farm",
            device_ids=("wtg1",),
            point_table_id="point_table_wtg_v1.0.0",
            data_level=pb.STORAGE_DATA_LEVEL_PROTOCOL,
            start_time=Timestamp(seconds=1795607900),
            end_time=Timestamp(seconds=1795608100),
            point_ids=("value",),
            schema_fingerprint=schema.fingerprint,
        ),
        page_size=10,
    )
    return await StorageServicer.QuerySamples.__wrapped__(service, request, Mock())


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
async def test_grouped_rpc_rejects_out_of_range_physical_values(
    sql_type: str,
    value: object,
) -> None:
    with pytest.raises(TDengineSchemaConflictError):
        await _read(sql_type, value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sql_type", "value", "field"),
    [
        ("TINYINT", -128, "value_i64"),
        ("TINYINT", 127, "value_i64"),
        ("BIGINT", -(2**63), "value_i64"),
        ("BIGINT UNSIGNED", 2**64 - 1, "value_u64"),
        ("FLOAT", 1.25, "value_f64"),
        ("VARCHAR(6)", "你好", "value_text"),
        ("VARBINARY(3)", b"abc", "payload_bytes"),
    ],
)
async def test_grouped_rpc_preserves_valid_boundary_values(
    sql_type: str,
    value: object,
    field: str,
) -> None:
    result = await _read(sql_type, value)
    point = result.groups[0].rows[0].values[0]
    assert point.WhichOneof("value") == field
    assert getattr(point, field) == value


@pytest.mark.asyncio
async def test_grouped_rpc_rejects_schema_mismatch_without_partial_success() -> None:
    with pytest.raises(TDengineSchemaConflictError, match="fingerprint mismatch"):
        await _read("INT", 1, wrong_fingerprint=True)
