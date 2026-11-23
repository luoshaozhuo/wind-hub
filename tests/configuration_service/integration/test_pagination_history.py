"""Exact last-page and historical-watermark pagination checks."""

from __future__ import annotations

import pytest
import yaml

from contracts import configuration_repository_pb2 as pb
from tests.configuration_service.integration.test_repository_rpc import document
from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


async def _stage_and_publish(repository, request_id: str, revision: int, documents):
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
    return result


def _with_source(item, dependency: str) -> pb.ConfigDocumentInput:
    payload = yaml.safe_load(item.yaml_bytes)
    payload["config_sources"] = [
        {
            "config_type": "collector_tasks",
            "config_id": "history",
            "file_version": dependency,
        }
    ]
    item.yaml_bytes = yaml.safe_dump(payload).encode()
    return item


@pytest.mark.asyncio
async def test_versions_and_references_last_page_and_version_watermark(
    live: LiveConfiguration,
) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        identity = pb.ConfigId(config_type="collector_tasks", config_id="history")
        v1 = document("history", "1.0.0")
        published_v1 = await _stage_and_publish(repository, "history-v1", 0, [v1])
        revision = published_v1.revision_id
        version1 = published_v1.published[0].version_id

        v2 = _with_source(document("history", "2.0.0"), "1.0.0")
        published_v2 = await _stage_and_publish(repository, "history-v2", revision, [v1, v2])
        revision = published_v2.revision_id
        version2 = next(
            item.version_id for item in published_v2.published if item.file_version == "2.0.0"
        )

        first = await repository.ListConfigVersions(
            pb.ListConfigVersionsRequest(config_id=identity, page_size=1, include_deprecated=True)
        )
        assert [item.version_id for item in first.versions] == [version1]
        assert first.next_page_token

        # A standalone new root retires the older files from the active
        # directory while keeping every immutable version queryable.
        published_v3 = await _stage_and_publish(
            repository, "history-v3", revision, [document("history", "3.0.0")]
        )
        revision = published_v3.revision_id
        await repository.DeprecateConfigVersion(
            pb.DeprecateConfigVersionRequest(
                request_id="history-deprecate",
                version_id=version2,
                reason="pagination watermark",
                expected_revision_id=revision,
            )
        )
        # The version watermark is frozen by the first page cursor, so open a
        # fresh first page after the deprecation revision and walk every page.
        page = await repository.ListConfigVersions(
            pb.ListConfigVersionsRequest(
                config_id=identity,
                page_size=1,
                include_deprecated=True,
            )
        )
        seen: list[pb.ConfigVersionSummary] = list(page.versions)
        token = page.next_page_token
        while token:
            page = await repository.ListConfigVersions(
                pb.ListConfigVersionsRequest(
                    config_id=identity,
                    page_size=1,
                    page_token=token,
                    include_deprecated=True,
                )
            )
            seen.extend(page.versions)
            token = page.next_page_token
        assert token == ""
        assert [item.version_id for item in seen] == [
            version1,
            version2,
            published_v3.published[0].version_id,
        ]
        v2_summary = next(item for item in seen if item.version_id == version2)
        assert v2_summary.deprecated
        references = await repository.ListReferences(
            pb.ListReferencesRequest(
                version_id=version1,
                direction=pb.REFERENCE_DIRECTION_INBOUND,
                page_size=1,
            )
        )
        assert len(references.edges) == 1
        assert references.next_page_token == ""
    finally:
        await channel.close()
