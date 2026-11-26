"""Authenticated Storage QuerySamples handler exposes exact physical versions."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as pb
from storage_service.application.wide_group_query import GroupedWidePage, GroupedWideRows
from storage_service.application.wide_query import WideHistoryRow
from storage_service.domain.quality_bitmap import PointQuality, unpack_quality_bitmap
from storage_service.domain.wide_record import WideDataLevel, WideIdentity
from storage_service.domain.wide_schema import PhysicalPoint, WideSchema
from storage_service.infrastructure.adapters.inbound.grpc_server import StorageServicer


@pytest.mark.asyncio
async def test_exact_grouped_rpc_binds_auth_schema_quality_and_unsigned_value() -> None:
    service = object.__new__(StorageServicer)
    service._auth = Mock()
    service._consumer = SimpleNamespace(recovering=False)
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
    identity = WideIdentity("farm", "device_farm_v1.0.0", "wtg1", WideDataLevel.PROTOCOL)
    service._point_tables = Mock()
    service._point_tables.get_wide_column_metadata = AsyncMock(
        side_effect=[
            ("power", "active_power", "uint64", "kilowatt"),
            ("wind_speed", "wind", "float", "m/s"),
        ]
    )
    service._grouped_query = Mock()
    service._grouped_query.query = AsyncMock(
        return_value=GroupedWidePage(
            groups=(
                GroupedWideRows(
                    identity=identity,
                    device_type="turbine",
                    schema=schema,
                    point_ids=("power", "wind_speed"),
                    rows=(
                        WideHistoryRow(
                            1795608000000000,
                            (
                                ("power", 2**64 - 1, PointQuality.GOOD),
                                ("wind_speed", None, PointQuality.NOT_COLLECTED),
                            ),
                        ),
                    ),
                ),
            ),
            next_page_token="",
            complete=True,
        )
    )
    context = Mock()
    request = pb.QuerySamplesRequest(
        filter=pb.StorageFilter(
            wind_farm_id="farm",
            device_type="turbine",
            device_ids=("wtg1",),
            point_table_id="point_table_wtg_v1.0.0",
            data_level=pb.STORAGE_DATA_LEVEL_PROTOCOL,
            start_time=Timestamp(seconds=1795607900),
            end_time=Timestamp(seconds=1795608100),
            point_ids=("power", "wind_speed"),
        ),
        page_size=10,
    )
    result = await service.QuerySamples(request, context)
    service._auth.require.assert_called_once_with(context, permission="storage.read", farm="farm")
    assert len(result.groups) == 1
    group = result.groups[0]
    assert group.point_table_id == "point_table_wtg_v1.0.0"
    assert group.schema_fingerprint == schema.fingerprint
    assert group.device_type == "turbine"
    assert group.rows[0].values[0].value_u64 == 2**64 - 1
    assert group.rows[0].values[1].WhichOneof("value") is None
    assert unpack_quality_bitmap(group.rows[0].quality, 2) == (
        PointQuality.GOOD,
        PointQuality.NOT_COLLECTED,
    )
    assert group.columns[0].business_point_id == "active_power"
    assert group.columns[0].unit == "kilowatt"
    assert group.columns[1].physical_name == schema.columns[1].column_name
    selector = service._grouped_query.query.await_args.kwargs["selector"]
    assert selector.device_ids == ("wtg1",)
    assert selector.device_type == "turbine"
    assert selector.point_table_id == "point_table_wtg_v1.0.0"
