"""All twelve Repository RPCs over the real CLI-launched Bearer service."""

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


def document(config_id: str, version: str, *, name: str | None = None) -> pb.ConfigDocumentInput:
    payload = {
        "schema_version": 1,
        "file_version": version,
        "site_id": config_id,
        "name": name or config_id,
    }
    return pb.ConfigDocumentInput(
        config_id=pb.ConfigId(config_type="site", config_id=config_id),
        file_name=f"{config_id}.yaml",
        yaml_bytes=yaml.safe_dump(payload, sort_keys=False).encode(),
    )


@pytest.mark.asyncio
async def test_repository_rpc_success_matrix(live: LiveConfiguration) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        initial = await repository.ListConfigs(pb.ListConfigsRequest(), timeout=10)
        assert initial.snapshot_revision_id == 0
        validated = await repository.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id="rpc-validate",
                expected_revision_id=0,
                documents=[document("rpc-site", "1.0.0")],
                validate_only=True,
            ),
            timeout=10,
        )
        assert validated.valid and not validated.candidate_id
        assert validated.document_results[0].content_sha256
        staged = await repository.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id="rpc-create-v1",
                expected_revision_id=0,
                documents=[document("rpc-site", "1.0.0"), document("rpc-base", "1.0.0")],
            ),
            timeout=10,
        )
        assert staged.valid and staged.candidate_id
        published_v1 = await repository.PublishCandidate(
            pb.PublishCandidateRequest(
                request_id="rpc-publish-v1",
                candidate_id=staged.candidate_id,
                expected_revision_id=0,
            ),
            timeout=10,
        )
        assert published_v1.revision_id == 1
        assert published_v1.committed_at.seconds > 0
        version1 = next(
            item.version_id
            for item in published_v1.published
            if item.config_id.config_id == "rpc-site"
        )
        base_version = next(
            item.version_id
            for item in published_v1.published
            if item.config_id.config_id == "rpc-base"
        )
        listed = await repository.ListConfigs(pb.ListConfigsRequest(config_type="site"), timeout=10)
        listed_site = next(item for item in listed.items if item.config_id.config_id == "rpc-site")
        assert listed_site.recommended_version_id == version1
        versions = await repository.ListConfigVersions(
            pb.ListConfigVersionsRequest(
                config_id=pb.ConfigId(config_type="site", config_id="rpc-site")
            ),
            timeout=10,
        )
        assert [item.version_id for item in versions.versions] == [version1]
        fetched = await repository.GetConfig(
            pb.GetConfigRequest(
                config_id=pb.ConfigId(config_type="site", config_id="rpc-site"),
                version_id=version1,
            ),
            timeout=10,
        )
        assert (
            hashlib.sha256(fetched.document.yaml_bytes).hexdigest() == fetched.document.yaml_sha256
        )
        assert yaml.safe_load(fetched.document.yaml_bytes)["name"] == "rpc-site"
        bundle_v1 = await repository.GetConfigBundle(
            pb.GetConfigBundleRequest(
                roots=[pb.ConfigId(config_type="site", config_id="rpc-site")],
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
        dependent = document("rpc-site", "2.0.0", name="rpc-site-v2")
        payload = yaml.safe_load(dependent.yaml_bytes)
        payload["config_sources"] = [
            {
                "config_type": "site",
                "config_id": "rpc-base",
                "file_version": "1.0.0",
            }
        ]
        dependent.yaml_bytes = yaml.safe_dump(payload, sort_keys=False).encode()
        staged_v2 = await repository.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id="rpc-create-v2",
                expected_revision_id=1,
                documents=[dependent, document("rpc-base", "1.0.0")],
            ),
            timeout=10,
        )
        published_v2 = await repository.PublishCandidate(
            pb.PublishCandidateRequest(
                request_id="rpc-publish-v2",
                candidate_id=staged_v2.candidate_id,
                expected_revision_id=1,
            ),
            timeout=10,
        )
        version2 = next(
            item.version_id
            for item in published_v2.published
            if item.config_id.config_id == "rpc-site"
        )
        bundle = await repository.GetConfigBundle(
            pb.GetConfigBundleRequest(
                roots=[pb.ConfigId(config_type="site", config_id="rpc-site")],
                revision_id=2,
                include_dependencies=True,
            ),
            timeout=10,
        )
        assert {item.version_id for item in bundle.files} == {base_version, version2}
        assert bundle.files[0].version_id == base_version
        outbound = await repository.ListReferences(
            pb.ListReferencesRequest(
                version_id=version2,
                direction=pb.REFERENCE_DIRECTION_OUTBOUND,
            ),
            timeout=10,
        )
        assert outbound.edges[0].target_version_id == base_version
        inbound = await repository.ListReferences(
            pb.ListReferencesRequest(
                version_id=base_version,
                direction=pb.REFERENCE_DIRECTION_INBOUND,
            ),
            timeout=10,
        )
        assert inbound.edges[0].source_version_id == version2
        recommended = await repository.SetRecommendedVersion(
            pb.SetRecommendedVersionRequest(
                request_id="rpc-recommend-v1",
                config_id=pb.ConfigId(config_type="site", config_id="rpc-site"),
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
                config_id=pb.ConfigId(config_type="site", config_id="rpc-site"),
                version_id=version2,
                expected_revision_id=3,
            ),
            timeout=10,
        )
        deprecated = await repository.DeprecateConfigVersion(
            pb.DeprecateConfigVersionRequest(
                request_id="rpc-deprecate-v1",
                version_id=version1,
                reason="superseded",
                expected_revision_id=4,
            ),
            timeout=10,
        )
        assert deprecated.deprecated and deprecated.referencing_versions_count == 0
        stats = await repository.GetRepositoryStats(
            pb.GetRepositoryStatsRequest(
                scope=pb.REPOSITORY_STATS_SCOPE_ALL,
                include_storage=True,
            ),
            timeout=10,
        )
        assert stats.version_count == 3
        assert stats.revision_count == 5
        assert stats.db_schema_version == 1
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
        replay = await repository.PublishCandidate(
            pb.PublishCandidateRequest(
                request_id="rpc-publish-v1",
                candidate_id=staged.candidate_id,
                expected_revision_id=0,
            ),
            timeout=10,
        )
        assert replay.already_applied
        assert replay.revision_id == published_v1.revision_id
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
                    config_id=pb.ConfigId(config_type="site", config_id="rpc-site")
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
                        config_id=pb.ConfigId(config_type="site", config_id="invalid"),
                        file_name="invalid.yaml",
                        yaml_bytes=b"a: [",
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
            staged = await repository.CreateCandidate(
                pb.CreateCandidateRequest(
                    request_id="pagination-create",
                    expected_revision_id=revision,
                    documents=[
                        document("pagination-a", "1.0.0"),
                        document("pagination-b", "1.0.0"),
                    ],
                ),
                timeout=10,
            )
            await repository.PublishCandidate(
                pb.PublishCandidateRequest(
                    request_id="pagination-publish",
                    candidate_id=staged.candidate_id,
                    expected_revision_id=revision,
                ),
                timeout=10,
            )
        first = await repository.ListConfigs(pb.ListConfigsRequest(page_size=1), timeout=10)
        assert len(first.items) == 1
        assert first.next_page_token
        token = first.next_page_token
        collected = [item.config_id.config_id for item in first.items]
        for _ in range(5):
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
