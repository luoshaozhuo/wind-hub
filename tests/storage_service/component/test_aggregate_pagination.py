"""聚合分页必须绑定不可变快照，并受进程级内存与生命周期约束。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as pb
from storage_service.application.config import StorageConfig, load_storage_config
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.application.query import HistoryPage, HistoryRow
from storage_service.infrastructure.adapters.inbound import grpc_server
from storage_service.infrastructure.adapters.inbound.grpc_server import StorageServicer

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "storage-component-token-0123456789012345678901234567890123456789"
pytestmark = pytest.mark.component


class _AbortError(Exception):
    def __init__(self, code: grpc.StatusCode) -> None:
        self.code = code


class _Context:
    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return (("authorization", f"Bearer {TOKEN}"),)

    async def abort(self, code: grpc.StatusCode, detail: str) -> None:
        raise _AbortError(code)


class _Query:
    def __init__(self, rows: list[HistoryRow]) -> None:
        self.rows = rows
        self.calls = 0

    async def query(self, filter_: object, *, page_size: int, page_token: str) -> HistoryPage:
        self.calls += 1
        return HistoryPage(rows=tuple(self.rows), next_page_token="", complete=True)


class _Units:
    async def get_unit(self, **kwargs: object) -> str:
        return ""


def _config() -> StorageConfig:
    return load_storage_config(
        (ROOT / "configs/services/service_storage-main_v1.1.0.yaml").read_bytes()
    )


def _rows(count: int = 4) -> list[HistoryRow]:
    return [
        HistoryRow(
            ts_us=index * 1_000_000,
            logical_key=f"{index:064x}",
            content_sha256=f"{index + 1:064x}",
            identity=("farm-a", "turbine", "device-1", "table-1", "raw", "schema"),
            sample_id=f"sample-{index}",
            point_id="point-1",
            business_point_id="",
            value_kind=pb.STORAGE_VALUE_KIND_INT64,
            value=index + 1,
            quality="good",
            publish_time_us=index * 1_000_000,
            receive_time_us=None,
        )
        for index in range(count)
    ]


def _servicer(monkeypatch: pytest.MonkeyPatch, query: _Query) -> StorageServicer:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    return StorageServicer(
        config=_config(),
        metadata=cast(Any, object()),
        query=cast(Any, query),
        wide_query=cast(Any, object()),
        wide_aggregation=cast(Any, object()),
        jobs=cast(Any, object()),
        point_tables=cast(Any, _Units()),
        consumer=cast(Any, object()),
        cursor_secret=b"x" * 32,
        config_status=lambda: "READY",
        metrics=StorageRuntimeMetrics(),
    )


def _request(
    *, page_token: str = "", page_size: int = 2, point_id: str = ""
) -> pb.QueryAggregatesRequest:
    start = Timestamp(seconds=0)
    end = Timestamp(seconds=60)
    return pb.QueryAggregatesRequest(
        filter=pb.StorageFilter(
            wind_farm_id="farm-a",
            device_type="turbine",
            device_id="device-1",
            point_table_id="table-1",
            point_id=point_id,
            data_level=pb.STORAGE_DATA_LEVEL_RAW,
            start_time=start,
            end_time=end,
        ),
        aggregation=pb.STORAGE_AGGREGATION_AVG,
        window_ms=1000,
        page_size=page_size,
        page_token=page_token,
    )


async def test_backfill_cannot_change_following_aggregate_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _Query(_rows())
    servicer = _servicer(monkeypatch, query)
    context = cast(Any, _Context())
    first = await servicer.QueryAggregates(_request(), context)
    assert [item.result for item in first.items] == [1.0, 2.0]
    assert first.next_page_token

    query.rows = [
        HistoryRow(
            ts_us=500_000,
            logical_key="f" * 64,
            content_sha256="e" * 64,
            identity=("farm-a", "turbine", "device-1", "table-1", "raw", "schema"),
            sample_id="late",
            point_id="point-1",
            business_point_id="",
            value_kind=pb.STORAGE_VALUE_KIND_INT64,
            value=999,
            quality="good",
            publish_time_us=500_000,
            receive_time_us=None,
        ),
        *query.rows,
    ]
    second = await servicer.QueryAggregates(_request(page_token=first.next_page_token), context)
    assert [item.result for item in second.items] == [3.0, 4.0]
    assert not second.next_page_token
    assert query.calls == 1


async def test_snapshot_rejects_expiry_and_process_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    query = _Query(_rows())
    servicer = _servicer(monkeypatch, query)
    first = await servicer.QueryAggregates(_request(), cast(Any, _Context()))

    restarted = _servicer(monkeypatch, query)
    with pytest.raises(_AbortError) as unavailable:
        await restarted.QueryAggregates(
            _request(page_token=first.next_page_token), cast(Any, _Context())
        )
    assert unavailable.value.code == grpc.StatusCode.INVALID_ARGUMENT

    monkeypatch.setattr(time, "time", lambda: 1_901.0)
    with pytest.raises(_AbortError) as expired:
        await servicer.QueryAggregates(
            _request(page_token=first.next_page_token), cast(Any, _Context())
        )
    assert expired.value.code == grpc.StatusCode.INVALID_ARGUMENT


async def test_total_snapshot_budget_evicts_oldest_before_exceeding_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    query = _Query(_rows())
    servicer = _servicer(monkeypatch, query)
    context = cast(Any, _Context())
    first = await servicer.QueryAggregates(_request(point_id="filter-1"), context)
    first_id = next(iter(servicer._aggregate_snapshots))
    one_size = sum(len(row) for rows in servicer._aggregate_snapshots.values() for row in rows[2])
    monkeypatch.setattr(grpc_server, "_AGGREGATE_SNAPSHOT_MAX_BYTES", one_size * 2)

    await servicer.QueryAggregates(_request(point_id="filter-2"), context)
    assert len(servicer._aggregate_snapshots) == 2
    third = await servicer.QueryAggregates(_request(point_id="filter-3"), context)
    assert len(third.items) == 2
    assert first_id not in servicer._aggregate_snapshots
    assert len(servicer._aggregate_snapshots) == 2
    assert (
        sum(len(row) for value in servicer._aggregate_snapshots.values() for row in value[2])
        <= one_size * 2
    )

    with pytest.raises(_AbortError) as evicted:
        await servicer.QueryAggregates(_request(page_token=first.next_page_token), context)
    assert evicted.value.code == grpc.StatusCode.INVALID_ARGUMENT
