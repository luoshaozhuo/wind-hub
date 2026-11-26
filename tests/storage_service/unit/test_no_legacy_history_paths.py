"""The Storage transport refuses retired per-point aggregate/export filters."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from contracts import storage_service_pb2 as pb
from storage_service.application.query import QueryRejectedError
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
