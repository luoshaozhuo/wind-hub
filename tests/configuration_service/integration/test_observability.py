"""Measured RPC metrics, persistent audits and storage diagnostics."""

from __future__ import annotations

import asyncio
import sqlite3
import uuid

import grpc
import pytest

from contracts import common_pb2
from contracts import configuration_repository_pb2 as pb
from tests.configuration_service.integration.test_repository_rpc import document
from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


def values(response: common_pb2.GetMetricsResponse) -> dict[str, float]:
    return {metric.name: metric.value for metric in response.metrics}


@pytest.mark.asyncio
async def test_real_metrics_counts_latency_backup_states_and_rejected_audits(
    live: LiveConfiguration,
) -> None:
    repository, common, channel = live.stubs("deployer")
    try:
        initial = values(await common.GetMetrics(common_pb2.GetMetricsRequest(window_ms=60000)))
        assert initial["rpc_request_count"] == 0
        assert initial["rpc_average_latency_ms"] == 0
        for state in ("QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "INTERRUPTED"):
            task_id = uuid.uuid4().hex
            with sqlite3.connect(live.database_path) as connection:
                connection.execute(
                    "INSERT INTO backup_tasks "
                    "(task_id, actor_id, request_id, state, created_at) VALUES (?, ?, ?, ?, ?)",
                    (task_id, "fixture", task_id, state, "2026-01-01T00:00:00+00:00"),
                )

        async def success(index: int) -> None:
            response = await repository.CreateCandidate(
                pb.CreateCandidateRequest(
                    request_id=f"metric-{index}",
                    expected_revision_id=0,
                    documents=[document(f"metric-{index}", "1.0.0")],
                ),
                timeout=10,
            )
            assert response.valid

        await asyncio.gather(*(success(index) for index in range(8)))
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await repository.GetConfig(
                pb.GetConfigRequest(config_id=pb.ConfigId(config_type="site", config_id="missing")),
                timeout=10,
            )
        assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        measured = values(await common.GetMetrics(common_pb2.GetMetricsRequest(window_ms=60000)))
        assert measured["rpc_request_count"] >= 9
        assert measured["rpc_failed_count"] >= 1
        assert measured["rpc_requests_per_second"] > 0
        assert measured["rpc_average_latency_ms"] > 0
        assert measured["rpc_p95_latency_ms"] >= measured["rpc_average_latency_ms"]
        for state in ("queued", "running", "succeeded", "failed", "interrupted"):
            assert measured[f"backup_{state}"] == 1
        assert measured["requests_failed"] >= 1
        viewer_repository, _viewer_common, viewer_channel = live.stubs("viewer")
        try:
            with pytest.raises(grpc.aio.AioRpcError) as denied:
                await viewer_repository.CreateBackup(pb.CreateBackupRequest(request_id="denied"))
            assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
        finally:
            await viewer_channel.close()
        _repository, viewer_common, viewer_channel = live.stubs("viewer")
        try:
            with pytest.raises(grpc.aio.AioRpcError) as denied:
                await viewer_common.GetConfigSnapshot(
                    common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_DISK)
                )
            assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
        finally:
            await viewer_channel.close()
        backup_events = await common.GetEvents(
            common_pb2.GetEventsRequest(filter="CreateBackup", page_size=100)
        )
        common_events = await common.GetEvents(
            common_pb2.GetEventsRequest(filter="CommonService.Authorization", page_size=100)
        )
        messages = [event.message for event in (*backup_events.events, *common_events.events)]
        assert any("CreateBackup" in message for message in messages)
        assert any("CommonService.Authorization" in message for message in messages)
        assert all("PRIVATE KEY" not in message for message in messages)
        status = await common.GetServiceStatus(common_pb2.Empty())
        assert {item.name for item in status.dependencies} == {
            "sqlite",
            "references",
            "wal",
            "storage",
        }
        diagnosis = await common.Diagnose(common_pb2.DiagnoseRequest())
        assert {item.stage for item in diagnosis.stages} == {
            "sqlite",
            "references",
            "wal",
            "storage",
        }
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_backup_quota_rpc_is_resource_exhausted(live: LiveConfiguration) -> None:
    repository, _common, channel = live.stubs("deployer")
    try:
        with sqlite3.connect(live.database_path) as connection:
            for _ in range(2):
                task_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO backup_tasks "
                    "(task_id, actor_id, request_id, state, created_at) "
                    "VALUES (?, ?, ?, 'QUEUED', ?)",
                    (task_id, "fixture", task_id, "2026-01-01T00:00:00+00:00"),
                )
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await repository.CreateBackup(pb.CreateBackupRequest(request_id="backup-overflow"))
        assert rejected.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["removed", "replaced"])
async def test_database_loss_makes_health_and_diagnostics_fail_closed(
    live: LiveConfiguration, failure_mode: str
) -> None:
    _repository, common, channel = live.stubs("deployer")
    moved = live.database_path.with_suffix(f".{failure_mode}.original")
    sidecars = [
        live.database_path.with_name(live.database_path.name + suffix)
        for suffix in ("-wal", "-shm")
    ]
    healthy = await common.GetServiceStatus(common_pb2.Empty())
    assert healthy.health == "HEALTHY"
    with sqlite3.connect(live.database_path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    live.database_path.rename(moved)
    for sidecar in sidecars:
        if sidecar.exists():
            sidecar.rename(sidecar.with_suffix(sidecar.suffix + ".original"))
    try:
        if failure_mode == "removed":
            missing = await common.GetServiceStatus(common_pb2.Empty())
            assert missing.health == "UNHEALTHY", missing
        # Simulate SQLite or an external actor creating a different empty database.
        sqlite3.connect(live.database_path).close()
        status = await common.GetServiceStatus(common_pb2.Empty())
        assert status.health == "UNHEALTHY", status
        diagnosis = await common.Diagnose(common_pb2.DiagnoseRequest())
        assert diagnosis.overall_status == "FAIL"
    finally:
        live.database_path.unlink(missing_ok=True)
        for sidecar in sidecars:
            sidecar.unlink(missing_ok=True)
            original = sidecar.with_suffix(sidecar.suffix + ".original")
            if original.exists():
                original.rename(sidecar)
        moved.rename(live.database_path)
    restored = await common.GetServiceStatus(common_pb2.Empty())
    assert restored.health == "HEALTHY"
    await channel.close()
