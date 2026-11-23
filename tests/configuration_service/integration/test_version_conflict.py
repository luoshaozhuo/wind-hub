"""Immutable file-version conflicts are rejected as early as candidate staging."""

from __future__ import annotations

import grpc
import pytest

from contracts import configuration_repository_pb2 as pb
from tests.configuration_service.integration.test_repository_rpc import document
from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


@pytest.mark.asyncio
async def test_same_file_version_with_different_content_is_failed_precondition(
    live: LiveConfiguration,
) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        first = await repository.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id="version-first",
                expected_revision_id=0,
                documents=[document("versioned", "1.0.0", tasks={"sample": {"group": "first"}})],
            ),
            timeout=10,
        )
        await repository.PublishCandidate(
            pb.PublishCandidateRequest(
                request_id="version-first-publish",
                candidate_id=first.candidate_id,
                expected_revision_id=0,
            ),
            timeout=10,
        )
        # The immutable file_version guard runs while staging the directory,
        # before any candidate id or SQLite write is admitted.
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await repository.CreateCandidate(
                pb.CreateCandidateRequest(
                    request_id="version-conflict",
                    expected_revision_id=1,
                    documents=[
                        document("versioned", "1.0.0", tasks={"sample": {"group": "different"}})
                    ],
                ),
                timeout=10,
            )
        assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await channel.close()
