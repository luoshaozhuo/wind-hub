"""Authenticated Storage QueryWideRows handler exposes exact physical versions."""

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


@pytest.mark.asyncio
async def test_exact_wide_rpc_binds_auth_schema_quality_and_unsigned_value() -> None:
    service = object.__new__(StorageServicer)
    service._auth = Mock()
    service._consumer = SimpleNamespace(recovering=False)
    service._config = SimpleNamespace(
        tdengine=SimpleNamespace(type_mapping=Mock()),
    )
    schema = WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0",
        point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL,
        points=(
            PhysicalPoint("power", "BIGINT UNSIGNED"),
            PhysicalPoint("wind_speed", "FLOAT"),
        ),
        max_row_bytes=65536,
    )
    service._point_tables = Mock()
    service._point_tables.resolve_wide_schema = AsyncMock(return_value=schema)
    service._point_tables.wide_device_type.return_value = "turbine"
    service._wide_query = Mock()
    service._wide_query.query = AsyncMock(return_value=WideHistoryPage(
        rows=(WideHistoryRow(
            1795608000000000,
            (("power", 2**64 - 1, PointQuality.GOOD),
             ("wind_speed", None, PointQuality.NOT_COLLECTED)),
        ),),
        next_page_token="", complete=True, schema_fingerprint=schema.fingerprint,
    ))
    context = Mock()
    request = pb.QueryWideRowsRequest(
        wind_farm_id="farm", device_id="wtg1",
        device_config_id="device_farm_v1.0.0",
        data_level=pb.STORAGE_DATA_LEVEL_PROTOCOL,
        start_time=Timestamp(seconds=1795607900),
        end_time=Timestamp(seconds=1795608100),
        point_ids=("power", "wind_speed"),
        page_size=10,
    )
    result = await service.QueryWideRows(request, context)
    service._auth.require.assert_called_once()
    assert result.point_table_id == "point_table_wtg_v1.0.0"
    assert result.schema_fingerprint == schema.fingerprint
    assert result.rows[0].points[0].unsigned_value == 2**64 - 1
    assert result.rows[0].points[0].quality == 1
    assert result.rows[0].points[1].WhichOneof("value") is None
    assert result.rows[0].points[1].quality == 0
    assert result.columns[0].quality_ordinal == 0
    assert result.columns[1].quality_ordinal == 1
    assert service._wide_query.query.await_args.kwargs["device_type"] == "turbine"
