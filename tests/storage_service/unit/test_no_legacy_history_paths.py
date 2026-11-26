"""The Storage transport exposes only grouped v2 history and export filters."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import storage_service_pb2 as pb
from storage_service.domain.errors import QueryRejectedError
from storage_service.infrastructure.adapters.inbound.grpc_server import StorageServicer


def _incomplete_filter() -> pb.StorageFilter:
    return pb.StorageFilter(wind_farm_id="farm-a")


def _missing_level_filter() -> pb.StorageFilter:
    return pb.StorageFilter(
        wind_farm_id="farm-a",
        start_time=Timestamp(seconds=1),
        end_time=Timestamp(seconds=2),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method_name", "rpc_request", "message"),
    [
        (method, request, message)
        for method, request_type in (
            ("QuerySamples", pb.QuerySamplesRequest),
            ("QueryAggregates", pb.QueryAggregatesRequest),
        )
        for request, message in (
            (request_type(), "filter is required"),
            (request_type(filter=_incomplete_filter()), "level and UTC time range"),
            (request_type(filter=_missing_level_filter()), "level and UTC time range"),
        )
    ],
)
async def test_grouped_queries_reject_invalid_filters_with_authenticated_caller(
    method_name: str,
    rpc_request: pb.QuerySamplesRequest | pb.QueryAggregatesRequest,
    message: str,
) -> None:
    auth = Mock()
    servicer = SimpleNamespace(_auth=auth, _consumer=SimpleNamespace(recovering=False))
    method = getattr(StorageServicer, method_name)
    with pytest.raises(QueryRejectedError, match=message):
        await method.__wrapped__(servicer, rpc_request, Mock())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rpc_request", "message"),
    [
        (pb.CreateExportRequest(request_id="job-1"), "filter is required"),
        (
            pb.CreateExportRequest(request_id="job-1", filter=_incomplete_filter()),
            "level and UTC time range",
        ),
        (
            pb.CreateExportRequest(request_id="job-1", filter=_missing_level_filter()),
            "level and UTC time range",
        ),
    ],
)
async def test_export_rejects_invalid_filters_with_authenticated_caller(
    rpc_request: pb.CreateExportRequest,
    message: str,
) -> None:
    auth = Mock()
    jobs = Mock()
    servicer = SimpleNamespace(_auth=auth, _jobs=jobs)
    with pytest.raises(QueryRejectedError, match=message):
        await StorageServicer.CreateExport.__wrapped__(servicer, rpc_request, Mock())
    jobs.create_grouped_export.assert_not_called()
    jobs.create_export.assert_not_called()
    jobs.create_wide_export.assert_not_called()


def test_servicer_exposes_grouped_samples_and_no_legacy_point_query() -> None:
    assert "QuerySamples" in StorageServicer.__dict__
    assert "QueryWideRows" not in StorageServicer.__dict__
    assert not hasattr(StorageServicer, "_aggregate_page")
    assert not hasattr(StorageServicer, "_decode_aggregate_cursor")

    service = pb.DESCRIPTOR.services_by_name["StorageService"]
    assert "QuerySamples" in service.methods_by_name
    assert "QueryWideRows" not in service.methods_by_name
    assert not hasattr(pb, "StorageSample")
    assert not hasattr(pb, "QueryWideRowsRequest")
    assert not hasattr(pb, "QueryWideRowsResponse")


def test_retired_tdengine_methods_are_not_exposed() -> None:
    from storage_service.infrastructure.adapters.outbound.tdengine_backend import TDengineBackend

    for name in (
        "ensure_tables",
        "fetch_content_hashes",
        "insert_batch",
        "fetch_history",
        "_ensure_tables_sync",
        "_fetch_history_sync",
        "_decode_record",
    ):
        assert not hasattr(TDengineBackend, name), name


def test_config_resolver_does_not_offer_point_sample_compatibility() -> None:
    from storage_service.infrastructure.adapters.outbound.configuration_archive import (
        ConfigurationPointTableResolver,
    )

    assert not hasattr(ConfigurationPointTableResolver, "require_exact_versions")
    assert not hasattr(ConfigurationPointTableResolver, "get_unit")
    assert hasattr(ConfigurationPointTableResolver, "resolve_wide_schema")
    assert hasattr(ConfigurationPointTableResolver, "get_wide_column_metadata")


def test_production_storage_contains_no_point_history_implementation() -> None:
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[3] / "src" / "storage_service"
    forbidden = (
        re.compile(r"\bStorageSample\b"),
        re.compile(r"\bHistoryQueryService\b"),
        re.compile(r"\bQueryWideRows\b"),
        re.compile(r"\bfetch_content_hashes\b"),
        re.compile(r"\bensure_tables\b"),
        re.compile(r"\binsert_batch\b"),
        re.compile(r"\bIngestionService\b"),
        re.compile(r"storage_service\.domain\.sample"),
        re.compile(r"storage_service\.application\.wide_export\b"),
    )
    offenders = [
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if any(pattern.search(path.read_text(encoding="utf-8")) for pattern in forbidden)
    ]
    assert not offenders, f"retired point-history code paths: {sorted(offenders)}"
