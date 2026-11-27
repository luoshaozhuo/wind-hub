"""Storage 真实 gRPC CommonService 与全局授权边界。"""

from __future__ import annotations

import asyncio
import socket
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import storage_service_pb2 as storage_pb
from contracts import storage_service_pb2_grpc as storage_rpc
from storage_service.application.config import (
    DeviceBinding,
    DeviceModel,
    StorageConfig,
    load_storage_config,
)
from storage_service.application.metrics import StorageRuntimeMetrics
from storage_service.infrastructure.adapters.inbound.grpc_server import (
    AuthenticationError,
    AuthorizationError,
    AuthorizationPolicy,
    build_storage_grpc_server,
)
from storage_service.infrastructure.adapters.outbound.configuration_grpc import StorageBundle
from storage_service.infrastructure.adapters.outbound.configuration_snapshot import (
    StorageBundleSnapshot,
)

ROOT = Path(__file__).resolve().parents[3]
TOKEN = "storage-component-token-0123456789012345678901234567890123456789"
pytestmark = pytest.mark.component


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _config() -> StorageConfig:
    path = ROOT / "configs/services/service_storage-main_v2.0.1.yaml"
    config = load_storage_config(path.read_bytes())
    return config.model_copy(
        update={"grpc": config.grpc.model_copy(update={"listen_port": _port()})}
    )


def _server(
    config: StorageConfig,
    *,
    config_status: str = "READY",
    revisions: tuple[str, str] | list[str] | None = None,
    snapshot: StorageBundleSnapshot | None = None,
    metrics: StorageRuntimeMetrics | None = None,
    consumer: Any = None,
) -> grpc.aio.Server:
    return build_storage_grpc_server(
        config=config,
        metadata=cast(Any, object()),
        wide_query=cast(Any, object()),
        wide_aggregation=cast(Any, object()),
        grouped_query=cast(Any, object()),
        jobs=cast(Any, object()),
        point_tables=cast(Any, object()),
        consumer=consumer if consumer is not None else cast(Any, object()),
        cursor_secret=b"x" * 32,
        config_status=lambda: config_status,
        config_revisions=(lambda: revisions) if revisions is not None else None,
        config_snapshot=snapshot,
        metrics=metrics or StorageRuntimeMetrics(),
    )


def _bundle(config: StorageConfig, revision: int, target: str) -> StorageBundle:
    table_id = "point_table_turbine_v1.0.0"
    binding = DeviceBinding(
        wind_farm_id="farm-a",
        device_id="WTG01",
        device_config_id="farm-a",
        device_version_id="farm-a-1.0.0",
        device_file_version="1.0.0",
        site_config_id="site",
        site_file_version="1.0.0",
        model=DeviceModel(
            config_id="turbine",
            version_id="turbine-1.0.0",
            file_version="1.0.0",
            device_type_id="turbine",
            point_table_id=table_id,
        ),
    )
    return StorageBundle(
        configuration=config,
        revision_id=revision,
        deployment_id="prod-a",
        target_revision=target * 64,
        point_table_versions={table_id: ("turbine", "table-1.0.0")},
        business_points_pin=None,
        device_bindings=(binding,),
    )


def _limited(config: StorageConfig, permissions: list[str]) -> StorageConfig:
    caller = config.auth.callers[0].model_copy(update={"permissions": permissions})
    return config.model_copy(update={"auth": config.auth.model_copy(update={"callers": [caller]})})


class _Consumer:
    def __init__(self, *, recovering: bool = False) -> None:
        self.recovering = recovering


async def test_common_service_uses_bearer_and_reports_only_real_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    server = _server(config)
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = common_rpc.CommonServiceStub(channel)
    metadata = (("authorization", f"Bearer {TOKEN}"),)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as anonymous:
            await stub.GetServiceInfo(common.Empty())
        assert anonymous.value.code() == grpc.StatusCode.UNAUTHENTICATED

        info = await stub.GetServiceInfo(common.Empty(), metadata=metadata)
        assert info.service_id == "storage-main"
        assert info.process_role == "storage"
        status = await stub.GetServiceStatus(common.Empty(), metadata=metadata)
        assert status.health == "HEALTHY"
        capabilities = await stub.GetCapabilities(common.Empty(), metadata=metadata)
        assert "QuerySamples" in capabilities.rpc_names
        assert "QueryWideRows" not in capabilities.rpc_names
        assert "PushConfigSnapshot" not in capabilities.rpc_names

        metrics = await stub.GetMetrics(common.GetMetricsRequest(), metadata=metadata)
        assert {metric.name for metric in metrics.metrics} == {
            "storage.persisted_rows",
            "storage.persisted_points",
            "storage.confirmed_records",
            "storage.write_unknown",
            "storage.schema_conflicts",
            "storage.buffer_rows",
            "storage.buffer_bytes",
        }
        assert all(metric.value == 0 and metric.window_ms == 0 for metric in metrics.metrics)
        assert all(metric.sampled_at.seconds > 0 for metric in metrics.metrics)
    finally:
        await channel.close()
        await server.stop(grace=0)


async def test_every_storage_and_common_rpc_rejects_unauthorized_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Enumerate the live proto, not a frozen count or hand-written method list."""
    from google.protobuf.message_factory import GetMessageClass

    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    server = _server(config)
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    try:
        services = (
            (common.DESCRIPTOR, "CommonService", common_rpc.CommonServiceStub),
            (storage_pb.DESCRIPTOR, "StorageService", storage_rpc.StorageServiceStub),
        )
        observed: set[tuple[str, str]] = set()
        for file_descriptor, name, stub_cls in services:
            service = file_descriptor.services_by_name[name]
            stub = stub_cls(channel)
            for method in service.methods:
                assert not method.client_streaming and not method.server_streaming
                request = GetMessageClass(method.input_type)()
                for metadata in (
                    None,
                    (("authorization", "Bearer invalid-credential"),),
                ):
                    with pytest.raises(grpc.aio.AioRpcError) as rejected:
                        await getattr(stub, method.name)(request, metadata=metadata, timeout=5)
                    assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED, (
                        name,
                        method.name,
                    )
                observed.add((name, method.name))
        assert len(observed) == sum(
            len(descriptor.services_by_name[name].methods) for descriptor, name, _ in services
        )
    finally:
        await channel.close()
        await server.stop(grace=0)


class _Context:
    def __init__(self, token: str) -> None:
        self._metadata = (("authorization", f"Bearer {token}"),)

    def invocation_metadata(self) -> tuple[tuple[str, str], ...]:
        return self._metadata


def test_global_retry_requires_admin_and_invalid_token_is_unauthenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    limited_caller = config.auth.callers[0].model_copy(update={"permissions": ["storage.operate"]})
    limited = config.model_copy(
        update={"auth": config.auth.model_copy(update={"callers": [limited_caller]})}
    )
    policy = AuthorizationPolicy(limited)
    with pytest.raises(AuthorizationError):
        policy.require(
            cast(Any, _Context(TOKEN)),
            permission="storage.operate",
            farm=None,
            global_scope=True,
        )
    with pytest.raises(AuthenticationError):
        policy.require(
            cast(Any, _Context("wrong-token-012345678901234567890123456789")),
            permission="storage.operate",
            farm=None,
            global_scope=True,
        )


async def test_global_retry_is_permission_denied_for_farm_scoped_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    limited_caller = config.auth.callers[0].model_copy(update={"permissions": ["storage.operate"]})
    limited = config.model_copy(
        update={"auth": config.auth.model_copy(update={"callers": [limited_caller]})}
    )
    server = _server(limited)
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{limited.grpc.listen_port}")
    stub = storage_rpc.StorageServiceStub(channel)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await stub.RetryBlockedPartition(
                storage_pb.RetryBlockedPartitionRequest(request_id="retry-1"),
                metadata=(("authorization", f"Bearer {TOKEN}"),),
            )
        assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()
        await server.stop(grace=0)


class _RejectingConsumer:
    """Durable blocked evidence no longer matches: the retry must be refused, not crash."""

    async def retry_blocked_partition(
        self, *, request_id: str, topic: str, partition: int, expected_offset: int
    ) -> bool:
        return False


async def test_retry_blocked_partition_stale_offset_returns_failed_precondition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    server = build_storage_grpc_server(
        config=config,
        metadata=cast(Any, object()),
        wide_query=cast(Any, object()),
        wide_aggregation=cast(Any, object()),
        grouped_query=cast(Any, object()),
        jobs=cast(Any, object()),
        point_tables=cast(Any, object()),
        consumer=_RejectingConsumer(),
        cursor_secret=b"x" * 32,
        config_status=lambda: "READY",
        metrics=StorageRuntimeMetrics(),
    )
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = storage_rpc.StorageServiceStub(channel)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await stub.RetryBlockedPartition(
                storage_pb.RetryBlockedPartitionRequest(
                    request_id="retry-stale",
                    topic="windhub-history",
                    partition=0,
                    expected_blocked_offset=42,
                ),
                metadata=(("authorization", f"Bearer {TOKEN}"),),
            )
        assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await channel.close()
        await server.stop(grace=0)


async def test_opt_in_anonymous_status_is_redacted_and_does_not_grant_read_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    base = _config()
    config = base.model_copy(
        update={"grpc": base.grpc.model_copy(update={"allow_unauthenticated_status": True})}
    )
    server = _server(
        config,
        config_status="CONFIG_HISTORY_UNAVAILABLE",
        revisions=("a" * 64, "b" * 64),
    )
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    common_stub = common_rpc.CommonServiceStub(channel)
    storage_stub = storage_rpc.StorageServiceStub(channel)
    try:
        info = await common_stub.GetServiceInfo(common.Empty())
        assert info.process_role == "storage"
        assert info.contract_version == "windhub.storage.v2"
        status = await common_stub.GetServiceStatus(common.Empty())
        assert status.health == "DEGRADED"
        assert all(not dependency.detail for dependency in status.dependencies)
        assert not status.target_revision
        assert not status.active_revision
        authenticated_status = await common_stub.GetServiceStatus(
            common.Empty(),
            metadata=(("authorization", f"Bearer {TOKEN}"),),
        )
        assert authenticated_status.target_revision == "a" * 64
        assert authenticated_status.active_revision == "b" * 64
        capabilities = await common_stub.GetCapabilities(common.Empty())
        assert "QuerySamples" in capabilities.rpc_names
        assert "QueryWideRows" not in capabilities.rpc_names

        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await common_stub.GetServiceInfo(
                common.Empty(), metadata=(("authorization", "Bearer invalid"),)
            )
        assert rejected.value.code() == grpc.StatusCode.UNAUTHENTICATED

        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await storage_stub.GetStorageStats(storage_pb.GetStorageStatsRequest())
        assert denied.value.code() == grpc.StatusCode.UNAUTHENTICATED

        with pytest.raises(grpc.aio.AioRpcError) as denied_query:
            await storage_stub.QuerySamples(storage_pb.QuerySamplesRequest())
        assert denied_query.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await channel.close()
        await server.stop(grace=0)


async def test_metrics_report_real_counters_and_reject_windowed_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    metrics = StorageRuntimeMetrics()
    metrics.record_wide_persisted(rows=2, points_per_row=500)
    metrics.record_confirmed_records(3)
    metrics.record_unknown()
    metrics.record_schema_conflict()
    metrics.change_buffer(rows=4, byte_count=2048)
    config = _config()
    server = _server(config, metrics=metrics)
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = common_rpc.CommonServiceStub(channel)
    metadata = (("authorization", f"Bearer {TOKEN}"),)
    try:
        response = await stub.GetMetrics(
            common.GetMetricsRequest(scope="process"), metadata=metadata
        )
        values = {metric.name: metric.value for metric in response.metrics}
        assert values == {
            "storage.persisted_rows": 2,
            "storage.persisted_points": 1000,
            "storage.confirmed_records": 3,
            "storage.write_unknown": 1,
            "storage.schema_conflicts": 1,
            "storage.buffer_rows": 4,
            "storage.buffer_bytes": 2048,
        }
        for request in (
            common.GetMetricsRequest(scope="window"),
            common.GetMetricsRequest(window_ms=1000),
        ):
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.GetMetrics(request, metadata=metadata)
            assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await channel.close()
        await server.stop(grace=0)


async def test_config_snapshot_scopes_verify_digest_active_and_current_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    snapshot = StorageBundleSnapshot(tmp_path / "snapshots")
    active = _bundle(config, 1, "a")
    candidate = _bundle(config, 2, "b")
    snapshot.store(active)
    snapshot.store_target(candidate)
    revisions = [candidate.target_revision, active.target_revision]
    server = _server(
        config,
        snapshot=snapshot,
        revisions=revisions,
    )
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = common_rpc.CommonServiceStub(channel)
    metadata = (("authorization", f"Bearer {TOKEN}"),)
    try:
        for scope in (common.SNAPSHOT_SCOPE_DISK, common.SNAPSHOT_SCOPE_RUNTIME):
            response = await stub.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=scope), metadata=metadata
            )
            assert response.snapshot_id == active.target_revision
            assert response.files[0].sha256
            assert len(response.files[0].content) == response.files[0].size_bytes
        pending = await stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(
                scope=common.SNAPSHOT_SCOPE_CANDIDATE,
                candidate_id=candidate.target_revision,
            ),
            metadata=metadata,
        )
        assert pending.snapshot_id == candidate.target_revision
        with pytest.raises(grpc.aio.AioRpcError) as unknown:
            await stub.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(
                    scope=common.SNAPSHOT_SCOPE_CANDIDATE, candidate_id="c" * 64
                ),
                metadata=metadata,
            )
        assert unknown.value.code() == grpc.StatusCode.INVALID_ARGUMENT

        revisions[0] = "c" * 64
        with pytest.raises(grpc.aio.AioRpcError) as stale_target:
            await stub.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_CANDIDATE),
                metadata=metadata,
            )
        assert stale_target.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        revisions[0] = candidate.target_revision

        snapshot.store(candidate)
        revisions[1] = candidate.target_revision
        active_response = await stub.GetConfigSnapshot(
            common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME),
            metadata=metadata,
        )
        assert active_response.snapshot_id == candidate.target_revision
        with pytest.raises(grpc.aio.AioRpcError) as stale:
            await stub.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_CANDIDATE),
                metadata=metadata,
            )
        assert stale.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await channel.close()
        await server.stop(grace=0)


def test_metrics_events_are_threadsafe_and_bounded() -> None:
    metrics = StorageRuntimeMetrics()

    def write_events() -> None:
        for _ in range(100):
            metrics.record_unknown()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda _: write_events(), range(8)))

    events = metrics.events()
    assert metrics.snapshot().write_unknown == 800
    assert len(events) == 256
    assert [event.event_id for event in events] == list(range(545, 801))


async def test_events_are_bounded_filterable_paginated_and_cursor_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    metrics = StorageRuntimeMetrics()
    for _ in range(258):
        metrics.record_unknown()
    metrics.record_schema_conflict()
    config = _config()
    server = _server(config, metrics=metrics)
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = common_rpc.CommonServiceStub(channel)
    metadata = (("authorization", f"Bearer {TOKEN}"),)
    try:
        first = await stub.GetEvents(
            common.GetEventsRequest(page_size=100, filter="write_unknown"),
            metadata=metadata,
        )
        assert len(first.events) == 100
        assert first.next_page_token == "103"
        second = await stub.GetEvents(
            common.GetEventsRequest(page_size=100, page_token=first.next_page_token),
            metadata=metadata,
        )
        assert [int(event.event_id) for event in second.events] == list(range(104, 204))
        filtered = await stub.GetEvents(common.GetEventsRequest(filter="schema"), metadata=metadata)
        assert [event.error_code for event in filtered.events] == ["SCHEMA_CONFLICT"]

        retained = metrics.events()
        from_time = Timestamp()
        from_time.FromDatetime(retained[-1].timestamp)
        ranged = await stub.GetEvents(
            common.GetEventsRequest(from_time=from_time), metadata=metadata
        )
        assert [event.event_id for event in ranged.events] == [str(retained[-1].event_id)]
        with pytest.raises(grpc.aio.AioRpcError) as expired:
            await stub.GetEvents(common.GetEventsRequest(page_token="1"), metadata=metadata)
        assert expired.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        for request in (
            common.GetEventsRequest(page_size=501),
            common.GetEventsRequest(page_token="not-a-cursor"),
        ):
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.GetEvents(request, metadata=metadata)
            assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT

        concurrent = await asyncio.gather(
            *(
                stub.GetEvents(common.GetEventsRequest(page_size=5), metadata=metadata)
                for _ in range(8)
            )
        )
        assert all(
            [event.event_id for event in page.events] == ["4", "5", "6", "7", "8"]
            for page in concurrent
        )
    finally:
        await channel.close()
        await server.stop(grace=0)


async def test_config_snapshot_rejects_active_mismatch_and_tampering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    snapshot = StorageBundleSnapshot(tmp_path / "snapshots")
    active = _bundle(config, 1, "a")
    snapshot.store(active)
    server = _server(config, snapshot=snapshot, revisions=(active.target_revision, "b" * 64))
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = common_rpc.CommonServiceStub(channel)
    metadata = (("authorization", f"Bearer {TOKEN}"),)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as mismatch:
            await stub.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME),
                metadata=metadata,
            )
        assert mismatch.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await channel.close()
        await server.stop(grace=0)

    accepted_path = tmp_path / "snapshots" / "accepted_bundle.json"
    accepted_path.write_bytes(
        accepted_path.read_bytes().replace(b'"revision_id":1', b'"revision_id":2')
    )
    server = _server(
        config, snapshot=snapshot, revisions=(active.target_revision, active.target_revision)
    )
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = common_rpc.CommonServiceStub(channel)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as tampered:
            await stub.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK),
                metadata=metadata,
            )
        assert tampered.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await channel.close()
        await server.stop(grace=0)


async def test_diagnose_is_evidence_bounded_and_never_invents_dependency_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    server = _server(config, consumer=_Consumer(recovering=True))
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = common_rpc.CommonServiceStub(channel)
    metadata = (("authorization", f"Bearer {TOKEN}"),)
    try:
        response = await stub.Diagnose(common.DiagnoseRequest(), metadata=metadata)
        stages = {stage.stage: stage for stage in response.stages}
        assert stages["configuration"].status == "PASS"
        assert stages["kafka"].status == "UNKNOWN"
        assert stages["tdengine"].status == "UNKNOWN"
        assert stages["ingestion"].status == "UNKNOWN"
        assert "not actively probed" in stages["kafka"].message
        assert response.overall_status == "UNKNOWN"
        assert response.limitations
        for request in (
            common.DiagnoseRequest(stages=["unknown"]),
            common.DiagnoseRequest(stages=["kafka", "kafka"]),
            common.DiagnoseRequest(timeout_ms=5001),
            common.DiagnoseRequest(target_id="other-storage"),
        ):
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.Diagnose(request, metadata=metadata)
            assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    finally:
        await channel.close()
        await server.stop(grace=0)


async def test_common_read_write_permission_matrix_and_legacy_publication_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    metadata = (("authorization", f"Bearer {TOKEN}"),)
    for permissions in (["storage.inspect"], ["storage.operate"]):
        config = _limited(_config(), permissions)
        server = _server(config)
        await server.start()
        channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
        stub = common_rpc.CommonServiceStub(channel)
        try:
            if permissions == ["storage.inspect"]:
                with pytest.raises(grpc.aio.AioRpcError) as denied:
                    await stub.PushConfigSnapshot(
                        common.PushConfigSnapshotRequest(), metadata=metadata
                    )
                assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
            else:
                with pytest.raises(grpc.aio.AioRpcError) as denied:
                    await stub.GetMetrics(common.GetMetricsRequest(), metadata=metadata)
                assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
                for method, request in (
                    (stub.PushConfigSnapshot, common.PushConfigSnapshotRequest()),
                    (stub.ApplyConfigSnapshot, common.ApplyConfigSnapshotRequest()),
                ):
                    with pytest.raises(grpc.aio.AioRpcError) as unsupported:
                        await method(request, metadata=metadata)
                    assert unsupported.value.code() == grpc.StatusCode.UNIMPLEMENTED
        finally:
            await channel.close()
            await server.stop(grace=0)


async def test_config_snapshot_missing_accepted_or_candidate_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WIND_HUB_STORAGE_OPERATOR_TOKEN", TOKEN)
    config = _config()
    snapshot = StorageBundleSnapshot(tmp_path / "snapshots")
    server = _server(config, snapshot=snapshot)
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{config.grpc.listen_port}")
    stub = common_rpc.CommonServiceStub(channel)
    metadata = (("authorization", f"Bearer {TOKEN}"),)
    try:
        for scope in (common.SNAPSHOT_SCOPE_DISK, common.SNAPSHOT_SCOPE_CANDIDATE):
            with pytest.raises(grpc.aio.AioRpcError) as missing:
                await stub.GetConfigSnapshot(
                    common.GetConfigSnapshotRequest(scope=scope), metadata=metadata
                )
            assert missing.value.code() == grpc.StatusCode.FAILED_PRECONDITION
    finally:
        await channel.close()
        await server.stop(grace=0)
