"""Exact last-page and historical-watermark pagination checks."""

from __future__ import annotations

import pytest
import yaml

from contracts import configuration_repository_pb2 as pb
from tests.configuration_service.integration.test_repository_rpc import document
from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


async def publish(
    repository,
    request_id: str,
    version: str,
    revision: int,
    dependencies: tuple[str, ...] = (),
) -> tuple[int, str]:
    item = document("history", version)
    documents = [item]
    if dependencies:
        consumer = document("history-consumer", "1.0.0")
        payload = yaml.safe_load(consumer.yaml_bytes)
        payload["config_sources"] = [
            {"config_type": "site", "config_id": "history", "file_version": version}
        ]
        consumer.yaml_bytes = yaml.safe_dump(payload).encode()
        documents.append(consumer)
    staged = await repository.CreateCandidate(
        pb.CreateCandidateRequest(
            request_id=f"{request_id}-create",
            expected_revision_id=revision,
            documents=documents,
        )
    )
    result = await repository.PublishCandidate(
        pb.PublishCandidateRequest(
            request_id=f"{request_id}-publish",
            candidate_id=staged.candidate_id,
            expected_revision_id=revision,
        )
    )
    return result.revision_id, next(
        item.version_id for item in result.published if item.config_id.config_id == "history"
    )


@pytest.mark.asyncio
async def test_versions_and_references_last_page_and_version_watermark(
    live: LiveConfiguration,
) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        revision, version1 = await publish(repository, "history-v1", "1.0.0", 0)
        revision, version2 = await publish(repository, "history-v2", "2.0.0", revision)
        identity = pb.ConfigId(config_type="site", config_id="history")
        first = await repository.ListConfigVersions(
            pb.ListConfigVersionsRequest(config_id=identity, page_size=1)
        )
        assert [item.version_id for item in first.versions] == [version1]
        assert first.next_page_token
        revision, version3 = await publish(
            repository, "history-v3", "3.0.0", revision, ("1.0.0", "2.0.0")
        )
        await repository.DeprecateConfigVersion(
            pb.DeprecateConfigVersionRequest(
                request_id="history-deprecate",
                version_id=version2,
                reason="pagination watermark",
                expected_revision_id=revision,
            )
        )
        last = await repository.ListConfigVersions(
            pb.ListConfigVersionsRequest(
                config_id=identity,
                page_size=1,
                page_token=first.next_page_token,
            )
        )
        assert [item.version_id for item in last.versions] == [version2]
        assert not last.versions[0].deprecated
        assert last.next_page_token == ""
        references = await repository.ListReferences(
            pb.ListReferencesRequest(
                version_id=version3,
                direction=pb.REFERENCE_DIRECTION_INBOUND,
                page_size=1,
            )
        )
        assert len(references.edges) == 1
        assert references.next_page_token == ""
    finally:
        await channel.close()
