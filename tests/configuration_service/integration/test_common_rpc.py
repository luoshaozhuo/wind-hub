"""All nine Common RPCs and DISK/RUNTIME/CANDIDATE semantics."""

from __future__ import annotations

import hashlib

import grpc
import pytest
import yaml

from contracts import common_pb2 as pb
from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


@pytest.mark.asyncio
async def test_common_rpc_success_matrix(live: LiveConfiguration) -> None:
    _repository, common, channel = live.stubs("deployer")
    try:
        info = await common.GetServiceInfo(pb.Empty(), timeout=10)
        assert info.service_id == "configuration-integration"
        assert info.started_at.seconds > 0
        status = await common.GetServiceStatus(pb.Empty(), timeout=10)
        assert status.health == "HEALTHY"
        assert status.dependencies[0].name == "sqlite"
        capabilities = await common.GetCapabilities(pb.Empty(), timeout=10)
        # Fifteen repository RPCs plus the nine-method Common service surface.
        expected_rpc_names = {
            "CreateCandidate",
            "PublishCandidate",
            "ListConfigs",
            "ListConfigVersions",
            "GetConfig",
            "GetConfigBundle",
            "GetServiceConfigBundle",
            "ListReferences",
            "SetRecommendedVersion",
            "DeprecateConfigVersion",
            "GetRepositoryStats",
            "CreateBackup",
            "GetBackupTask",
            "GetCurrentRevision",
            "GetPointTableBundle",
            "GetServiceInfo",
            "GetServiceStatus",
            "GetCapabilities",
            "GetConfigSnapshot",
            "PushConfigSnapshot",
            "ApplyConfigSnapshot",
            "GetMetrics",
            "GetEvents",
            "Diagnose",
        }
        assert set(capabilities.rpc_names) == expected_rpc_names
        assert len(capabilities.rpc_names) == len(expected_rpc_names)
        assert "sqlite-online-backup" in capabilities.features
        disk = await common.GetConfigSnapshot(
            pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_DISK), timeout=10
        )
        runtime = await common.GetConfigSnapshot(
            pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_RUNTIME), timeout=10
        )
        assert disk.files[0].content == live.bootstrap_path.read_bytes()
        assert runtime.revision == disk.revision
        updated = yaml.safe_load(disk.files[0].content)
        updated["limits"]["max_workers"] += 1
        content = yaml.safe_dump(updated, sort_keys=False).encode()
        pushed = await common.PushConfigSnapshot(
            pb.PushConfigSnapshotRequest(
                files=[
                    pb.SnapshotFile(
                        relative_path=disk.files[0].relative_path,
                        content=content,
                        size_bytes=len(content),
                        sha256=hashlib.sha256(content).hexdigest(),
                    )
                ],
                expected_revision=disk.revision,
            ),
            timeout=10,
        )
        assert pushed.valid and pushed.candidate_id
        candidate = await common.GetConfigSnapshot(
            pb.GetConfigSnapshotRequest(
                scope=pb.SNAPSHOT_SCOPE_CANDIDATE,
                candidate_id=pushed.candidate_id,
            ),
            timeout=10,
        )
        assert candidate.files[0].content == content
        applied = await common.ApplyConfigSnapshot(
            pb.ApplyConfigSnapshotRequest(
                candidate_id=pushed.candidate_id,
                expected_revision=disk.revision,
            ),
            timeout=10,
        )
        assert applied.restart_required
        assert applied.apply_results[0].status == "RESTART_REQUIRED"
        runtime_after = await common.GetConfigSnapshot(
            pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_RUNTIME), timeout=10
        )
        disk_after = await common.GetConfigSnapshot(
            pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_DISK), timeout=10
        )
        assert runtime_after.files[0].content == runtime.files[0].content
        assert disk_after.files[0].content == content
        live.cli("service", "restart", "configuration", "--yes")
        await channel.close()
        _repository, common, channel = live.stubs("deployer")
        restarted_runtime = await common.GetConfigSnapshot(
            pb.GetConfigSnapshotRequest(scope=pb.SNAPSHOT_SCOPE_RUNTIME), timeout=10
        )
        assert restarted_runtime.revision == disk_after.revision
        invalid = await common.PushConfigSnapshot(
            pb.PushConfigSnapshotRequest(
                files=[
                    pb.SnapshotFile(
                        relative_path=disk.files[0].relative_path,
                        content=b"schema_version: 1\n",
                        size_bytes=len(b"schema_version: 1\n"),
                        sha256=hashlib.sha256(b"schema_version: 1\n").hexdigest(),
                    )
                ],
                expected_revision=disk_after.revision,
            ),
            timeout=10,
        )
        assert not invalid.valid
        metrics = await common.GetMetrics(pb.GetMetricsRequest(window_ms=1000), timeout=10)
        assert {item.name for item in metrics.metrics} >= {"document_count", "version_count"}
        events = await common.GetEvents(pb.GetEventsRequest(page_size=100), timeout=10)
        assert all("PRIVATE KEY" not in item.message for item in events.events)
        diagnosis = await common.Diagnose(pb.DiagnoseRequest(timeout_ms=1000), timeout=10)
        assert diagnosis.overall_status == "PASS"
        assert "sqlite" in {stage.stage for stage in diagnosis.stages}
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_common_snapshot_rejects_stale_candidate_and_bad_hash(
    live: LiveConfiguration,
) -> None:
    _repository, common, channel = live.stubs("deployer")
    try:
        with pytest.raises(grpc.aio.AioRpcError) as missing:
            await common.GetConfigSnapshot(
                pb.GetConfigSnapshotRequest(
                    scope=pb.SNAPSHOT_SCOPE_CANDIDATE,
                    candidate_id="missing",
                ),
                timeout=10,
            )
        assert missing.value.code() == grpc.StatusCode.NOT_FOUND
        with pytest.raises(grpc.aio.AioRpcError) as bad_hash:
            await common.PushConfigSnapshot(
                pb.PushConfigSnapshotRequest(
                    files=[
                        pb.SnapshotFile(
                            relative_path=live.bootstrap_path.name,
                            content=b"bad",
                            size_bytes=3,
                            sha256="0" * 64,
                        )
                    ]
                ),
                timeout=10,
            )
        assert bad_hash.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        with pytest.raises(grpc.aio.AioRpcError) as bad_scope:
            await common.GetConfigSnapshot(pb.GetConfigSnapshotRequest(), timeout=10)
        assert bad_scope.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await channel.close()
