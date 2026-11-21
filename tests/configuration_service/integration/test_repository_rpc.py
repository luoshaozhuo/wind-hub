"""All fourteen Repository RPCs over the real CLI-launched tokenized service."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import grpc
import pytest
import yaml

from contracts import configuration_repository_pb2 as pb
from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


def document(
    config_id: str,
    version: str,
    *,
    name: str | None = None,
    config_type: str = "collector_tasks",
    config_sources: list[dict[str, str]] | None = None,
    **fields: object,
) -> pb.ConfigDocumentInput:
    """Build a full-directory template document with a legal relative_path."""
    payload: dict[str, object] = {
        "schema_version": 1,
        "file_version": version,
        "tasks": {},
        **fields,
    }
    if config_sources is not None:
        payload["config_sources"] = config_sources
    relative_path = f"instances/collector_tasks_{config_id}_v{version}.yaml"
    return pb.ConfigDocumentInput(
        config_id=pb.ConfigId(config_type=config_type, config_id=config_id),
        file_name=relative_path.rsplit("/", 1)[-1],
        yaml_bytes=yaml.safe_dump(payload, sort_keys=False).encode(),
        relative_path=relative_path,
    )


async def _publish(
    repository: pb.ConfigurationRepositoryServiceStub,
    request_id: str,
    expected_revision: int,
    documents: list[pb.ConfigDocumentInput],
) -> pb.PublishCandidateResponse:
    staged = await repository.CreateCandidate(
        pb.CreateCandidateRequest(
            request_id=f"{request_id}-stage",
            expected_revision_id=expected_revision,
            documents=documents,
        ),
        timeout=10,
    )
    assert staged.valid and staged.candidate_id
    return await repository.PublishCandidate(
        pb.PublishCandidateRequest(
            request_id=f"{request_id}-publish",
            candidate_id=staged.candidate_id,
            expected_revision_id=expected_revision,
        ),
        timeout=10,
    )


@pytest.mark.asyncio
async def test_repository_rpc_success_matrix(live: LiveConfiguration) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        initial = await repository.ListConfigs(pb.ListConfigsRequest(), timeout=10)
        assert initial.snapshot_revision_id == 0
        doc_v1 = document("rpc-task", "1.0.0")
        validated = await repository.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id="rpc-validate",
                expected_revision_id=0,
                documents=[doc_v1],
                validate_only=True,
            ),
            timeout=10,
        )
        assert validated.valid and not validated.candidate_id
        assert validated.document_results[0].content_sha256
        staged_v1 = await repository.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id="rpc-v1-stage",
                expected_revision_id=0,
                documents=[doc_v1],
            ),
            timeout=10,
        )
        assert staged_v1.valid and staged_v1.candidate_id
        published_v1 = await repository.PublishCandidate(
            pb.PublishCandidateRequest(
                request_id="rpc-v1-publish",
                candidate_id=staged_v1.candidate_id,
                expected_revision_id=0,
            ),
            timeout=10,
        )
        assert published_v1.revision_id == 1
        assert published_v1.committed_at.seconds > 0
        version1 = published_v1.published[0].version_id
        listed = await repository.ListConfigs(pb.ListConfigsRequest(config_type="collector_tasks"), timeout=10)
        assert listed.items[0].recommended_version_id == version1
        versions = await repository.ListConfigVersions(
            pb.ListConfigVersionsRequest(
                config_id=pb.ConfigId(config_type="collector_tasks", config_id="rpc-task")
            ),
            timeout=10,
        )
        assert [item.version_id for item in versions.versions] == [version1]
        fetched = await repository.GetConfig(
            pb.GetConfigRequest(
                config_id=pb.ConfigId(config_type="collector_tasks", config_id="rpc-task"),
                version_id=version1,
            ),
            timeout=10,
        )
        assert (
            hashlib.sha256(fetched.document.yaml_bytes).hexdigest() == fetched.document.yaml_sha256
        )
        assert yaml.safe_load(fetched.document.yaml_bytes)["file_version"] == "1.0.0"
        bundle_v1 = await repository.GetConfigBundle(
            pb.GetConfigBundleRequest(
                roots=[pb.ConfigId(config_type="collector_tasks", config_id="rpc-task")],
                revision_id=1,
            ),
            timeout=10,
        )
        assert len(bundle_v1.files) == 1
        empty_references = await repository.ListReferences(
            pb.ListReferencesRequest(
                version_id=version1,
                direction=pb.REFERENCE_DIRECTION_OUTBOUND,
            ),
            timeout=10,
        )
        assert not empty_references.edges

        # Revision 2 keeps the immutable v1 in the complete root and adds v2,
        # which pins v1 as its exact dependency.
        doc_v2 = document(
            "rpc-task",
            "2.0.0",
            name="rpc-task-v2",
            config_sources=[
                {
                    "config_type": "collector_tasks",
                    "config_id": "rpc-task",
                    "file_version": "1.0.0",
                }
            ],
        )
        published_v2 = await _publish(repository, "rpc-v2", 1, [doc_v1, doc_v2])
        assert published_v2.revision_id == 2
        version2 = next(
            item.version_id for item in published_v2.published if item.file_version == "2.0.0"
        )
        bundle = await repository.GetConfigBundle(
            pb.GetConfigBundleRequest(
                roots=[pb.ConfigId(config_type="collector_tasks", config_id="rpc-task")],
                revision_id=2,
                include_dependencies=True,
            ),
            timeout=10,
        )
        assert {item.version_id for item in bundle.files} == {version1, version2}
        outbound = await repository.ListReferences(
            pb.ListReferencesRequest(
                version_id=version2,
                direction=pb.REFERENCE_DIRECTION_OUTBOUND,
            ),
            timeout=10,
        )
        assert outbound.edges[0].target_version_id == version1
        inbound = await repository.ListReferences(
            pb.ListReferencesRequest(
                version_id=version1,
                direction=pb.REFERENCE_DIRECTION_INBOUND,
            ),
            timeout=10,
        )
        assert inbound.edges[0].source_version_id == version2
        recommended = await repository.SetRecommendedVersion(
            pb.SetRecommendedVersionRequest(
                request_id="rpc-recommend-v1",
                config_id=pb.ConfigId(config_type="collector_tasks", config_id="rpc-task"),
                version_id=version1,
                expected_revision_id=2,
            ),
            timeout=10,
        )
        assert recommended.revision_id == 3
        assert recommended.previous_version_id == version2
        await repository.SetRecommendedVersion(
            pb.SetRecommendedVersionRequest(
                request_id="rpc-recommend-v2",
                config_id=pb.ConfigId(config_type="collector_tasks", config_id="rpc-task"),
                version_id=version2,
                expected_revision_id=3,
            ),
            timeout=10,
        )

        # A new root without v1 is required before the immutable v1 can be
        # deprecated; v3 does not carry the historical dependency.
        doc_v3 = document("rpc-task", "3.0.0", name="rpc-task-v3")
        published_v3 = await _publish(repository, "rpc-v3", 4, [doc_v3])
        assert published_v3.revision_id == 5
        deprecated = await repository.DeprecateConfigVersion(
            pb.DeprecateConfigVersionRequest(
                request_id="rpc-deprecate-v1",
                version_id=version1,
                reason="superseded",
                expected_revision_id=5,
            ),
            timeout=10,
        )
        assert deprecated.deprecated and deprecated.referencing_versions_count == 1
        stats = await repository.GetRepositoryStats(
            pb.GetRepositoryStatsRequest(
                scope=pb.REPOSITORY_STATS_SCOPE_ALL,
                include_storage=True,
            ),
            timeout=10,
        )
        assert stats.version_count == 3
        assert stats.revision_count == 6
        assert stats.db_schema_version == 2
        assert stats.HasField("db_file_bytes")
        backup = await repository.CreateBackup(
            pb.CreateBackupRequest(request_id="rpc-backup", label="integration"),
            timeout=10,
        )
        for _ in range(100):
            task = await repository.GetBackupTask(
                pb.GetBackupTaskRequest(task_id=backup.task_id), timeout=10
            )
            if task.task.state in (pb.BACKUP_STATE_SUCCEEDED, pb.BACKUP_STATE_FAILED):
                break
            await asyncio.sleep(0.05)
        assert task.task.state == pb.BACKUP_STATE_SUCCEEDED
        assert task.task.integrity_verified
        backup_bytes = Path(task.task.path).read_bytes()
        assert hashlib.sha256(backup_bytes).hexdigest() == task.task.sha256
        # Replaying the exact publish request is idempotent and returns the
        # original revision without creating a second one.
        replay = await repository.PublishCandidate(
            pb.PublishCandidateRequest(
                request_id="rpc-v1-publish",
                candidate_id=staged_v1.candidate_id,
                expected_revision_id=0,
            ),
            timeout=10,
        )
        assert replay.already_applied
        assert replay.revision_id == published_v1.revision_id

        current = await repository.GetCurrentRevision(pb.GetCurrentRevisionRequest(), timeout=10)
        assert current.revision_id == 6
        assert len(current.manifest_sha256) == 64
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_repository_rpc_failures_and_presence(live: LiveConfiguration) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        with pytest.raises(grpc.aio.AioRpcError) as missing_revision:
            await repository.CreateCandidate(
                pb.CreateCandidateRequest(request_id="missing-revision"),
                timeout=10,
            )
        assert missing_revision.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        with pytest.raises(grpc.aio.AioRpcError) as missing_selector:
            await repository.GetConfig(
                pb.GetConfigRequest(
                    config_id=pb.ConfigId(config_type="collector_tasks", config_id="rpc-task")
                ),
                timeout=10,
            )
        assert missing_selector.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        with pytest.raises(grpc.aio.AioRpcError) as bad_cursor:
            await repository.ListConfigs(pb.ListConfigsRequest(page_token="forged"), timeout=10)
        assert bad_cursor.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        invalid = await repository.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id="invalid-yaml",
                expected_revision_id=0,
                documents=[
                    pb.ConfigDocumentInput(
                        config_id=pb.ConfigId(config_type="collector_tasks", config_id="invalid"),
                        file_name="collector_tasks_invalid_v1.0.0.yaml",
                        yaml_bytes=b"a: [",
                        relative_path="instances/collector_tasks_invalid_v1.0.0.yaml",
                    )
                ],
                validate_only=True,
            ),
            timeout=10,
        )
        assert not invalid.valid
        assert invalid.issues[0].error_code == "SCHEMA_INVALID"
        with pytest.raises(grpc.aio.AioRpcError) as absent:
            await repository.GetBackupTask(pb.GetBackupTaskRequest(task_id="missing"), timeout=10)
        assert absent.value.code() == grpc.StatusCode.NOT_FOUND
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_pagination_terminates_at_exact_page_boundary(live: LiveConfiguration) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        existing = await repository.ListConfigs(pb.ListConfigsRequest(), timeout=10)
        if len(existing.items) < 2:
            revision = existing.snapshot_revision_id
            await _publish(
                repository,
                "pagination",
                revision,
                [
                    document("pagination-one", "1.0.0"),
                    document("pagination-two", "1.0.0"),
                ],
            )
        first = await repository.ListConfigs(pb.ListConfigsRequest(page_size=1), timeout=10)
        assert len(first.items) == 1
        assert first.next_page_token
        token = first.next_page_token
        collected = [item.config_id.config_id for item in first.items]
        for _ in range(10):
            page = await repository.ListConfigs(
                pb.ListConfigsRequest(page_size=1, page_token=token), timeout=10
            )
            collected.extend(item.config_id.config_id for item in page.items)
            token = page.next_page_token
            if not token:
                break
        assert token == ""
        assert len(collected) == len(set(collected))
        assert len(collected) >= 2
    finally:
        await channel.close()
