"""RPC request validation, quotas and stable pagination contracts."""

from __future__ import annotations

import grpc
import pytest

from contracts import configuration_repository_pb2 as pb
from tests.configuration_service.integration.test_repository_rpc import document
from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


@pytest.mark.asyncio
async def test_empty_request_id_is_invalid_argument(live: LiveConfiguration) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await repository.CreateCandidate(
                pb.CreateCandidateRequest(
                    request_id="",
                    expected_revision_id=0,
                    documents=[document("missing-request-id", "1.0.0")],
                ),
                timeout=10,
            )
        assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_document_quota_is_resource_exhausted(live: LiveConfiguration) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await repository.CreateCandidate(
                pb.CreateCandidateRequest(
                    request_id="too-many-documents",
                    expected_revision_id=0,
                    documents=[document(f"quota-{index}", "1.0.0") for index in range(101)],
                ),
                timeout=10,
            )
        assert rejected.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_candidate_quota_is_resource_exhausted(live: LiveConfiguration) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        for index in range(32):
            created = await repository.CreateCandidate(
                pb.CreateCandidateRequest(
                    request_id=f"candidate-quota-{index}",
                    expected_revision_id=0,
                    documents=[document(f"candidate-{index}", "1.0.0")],
                ),
                timeout=10,
            )
            assert created.valid
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await repository.CreateCandidate(
                pb.CreateCandidateRequest(
                    request_id="candidate-quota-overflow",
                    expected_revision_id=0,
                    documents=[document("candidate-overflow", "1.0.0")],
                ),
                timeout=10,
            )
        assert rejected.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        await channel.close()
