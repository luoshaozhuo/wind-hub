"""The Storage transport refuses retired per-point aggregate/export filters."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from contracts import storage_service_pb2 as pb
from storage_service.domain.errors import QueryRejectedError
from storage_service.infrastructure.adapters.inbound.grpc_server import StorageServicer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request",
    [
        pb.QueryAggregatesRequest(),
        pb.QueryAggregatesRequest(filter=pb.StorageFilter(wind_farm_id="farm-a")),
        pb.QueryAggregatesRequest(
            filter=pb.StorageFilter(wind_farm_id="farm-a"),
            wide_filter=pb.QueryWideRowsRequest(wind_farm_id="farm-a"),
        ),
    ],
)
async def test_aggregate_rejects_narrow_or_ambiguous_filter(
    request: pb.QueryAggregatesRequest,
) -> None:
    auth = Mock()
    servicer = SimpleNamespace(_auth=auth)
    with pytest.raises(QueryRejectedError, match="exact device filter"):
        await StorageServicer.QueryAggregates.__wrapped__(
            servicer, request, Mock()
        )
    auth.require.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request",
    [
        pb.CreateExportRequest(request_id="job-1"),
        pb.CreateExportRequest(
            request_id="job-1",
            filter=pb.StorageFilter(wind_farm_id="farm-a"),
        ),
        pb.CreateExportRequest(
            request_id="job-1",
            filter=pb.StorageFilter(wind_farm_id="farm-a"),
            wide_filter=pb.QueryWideRowsRequest(wind_farm_id="farm-a"),
        ),
    ],
)
async def test_export_rejects_narrow_or_ambiguous_filter(
    request: pb.CreateExportRequest,
) -> None:
    auth = Mock()
    jobs = Mock()
    servicer = SimpleNamespace(_auth=auth, _jobs=jobs)
    with pytest.raises(QueryRejectedError, match="exact device filter"):
        await StorageServicer.CreateExport.__wrapped__(
            servicer, request, Mock()
        )
    auth.require.assert_not_called()
    jobs.create_export.assert_not_called()
    jobs.create_wide_export.assert_not_called()


def test_servicer_exposes_no_legacy_point_query() -> None:
    assert "QuerySamples" not in StorageServicer.__dict__
    assert not hasattr(StorageServicer, "_aggregate_page")
    assert not hasattr(StorageServicer, "_decode_aggregate_cursor")


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
    assert hasattr(ConfigurationPointTableResolver, "get_wide_unit")


def test_production_storage_contains_no_point_history_implementation() -> None:
    from pathlib import Path
    import re

    src = Path(__file__).resolve().parents[3] / "src" / "storage_service"
    forbidden = (
        re.compile(r"\\bStorageSample\\b"),
        re.compile(r"\\bHistoryQueryService\\b"),
        re.compile(r"\\bQuerySamples\\b"),
        re.compile(r"\\bfetch_content_hashes\\b"),
        re.compile(r"\\bensure_tables\\b"),
        re.compile(r"\\binsert_batch\\b"),
        re.compile(r"\\bIngestionService\\b"),
        re.compile(r"storage_service\\.domain\\.sample"),
    )
    offenders = [
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if any(pattern.search(path.read_text(encoding="utf-8")) for pattern in forbidden)
    ]
    assert not offenders, f"retired point-history code paths: {sorted(offenders)}"
