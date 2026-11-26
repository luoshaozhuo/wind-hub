"""Compose the production v3 decoder, binder and query validator (fake SQL I/O)."""
import asyncio
import json
from unittest.mock import AsyncMock
from storage_service.domain.wide_record import WideRecord, WideDataLevel
from storage_service.domain.wide_schema import WideSchema, PhysicalPoint
from storage_service.domain.wide_row import bind_wide_row
from storage_service.application.wide_query import (
    WideHistoryQueryService, WideCursorCodec, WideHistoryFilter, WideHistoryRow,
)

async def main():
    schema = WideSchema.build(
        point_table_id="point_table_wtg_v1.0.0", point_table_version="1.0.0",
        data_level=WideDataLevel.PROTOCOL, points=(PhysicalPoint("power", "INT"),),
        max_row_bytes=65536,
    )
    payload = {
        "schema_version": 3, "wind_farm_id": "farm",
        "device_config_id": "device_farm_v1.0.0", "device_id": "wtg001",
        "data_level": "protocol", "sample_time": "2026-11-25T12:00:00Z",
        "points": {"power": [None, 1]},
    }
    row = WideRecord.from_kafka_json(json.dumps(payload).encode(), physical_points=("power",))
    bound = bind_wide_row(row, schema)
    backend = AsyncMock()
    backend.fetch_wide_history.return_value = (
        WideHistoryRow(row.sample_time_us, (("power", None, 1),)),
    )
    service = WideHistoryQueryService(backend=backend, codec=WideCursorCodec(b"x" * 32))
    print("DECODE/BIND accepted:", bound.values, bound.quality_bitmap.hex())
    try:
        await service.query(
            schema=schema, device_type="wind",
            filter_=WideHistoryFilter(
                wind_farm_id="farm", device_id="wtg001",
                device_config_id="device_farm_v1.0.0", data_level=WideDataLevel.PROTOCOL,
                start_time_us=row.sample_time_us - 1, end_time_us=row.sample_time_us + 1,
                point_ids=("power",), limit=10,
            ),
        )
    except RuntimeError as error:
        print("QUERY rejected:", error)
    else:
        raise AssertionError("D5 reproduction no longer reproduces; review the fix")

asyncio.run(main())
