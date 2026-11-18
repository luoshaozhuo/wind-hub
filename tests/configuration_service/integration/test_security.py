"""Real mTLS identity, authorization and fail-closed transport tests."""

from __future__ import annotations

import grpc
import pytest

from contracts import common_pb2, common_pb2_grpc
from contracts import configuration_repository_pb2 as pb
from tests.configuration_service.integration.test_repository_rpc import document
from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service, pytest.mark.security]


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["viewer", "ops", "deployer"])
async def test_query_permission_matrix(live: LiveConfiguration, identity: str) -> None:
    _repository, common, channel = live.stubs(identity)
    try:
        status = await common.GetServiceStatus(common_pb2.Empty(), timeout=10)
        assert status.health == "HEALTHY"
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_query_identity_cannot_publish(live: LiveConfiguration) -> None:
    repository, _common, channel = live.stubs("viewer")
    try:
        with pytest.raises(grpc.aio.AioRpcError) as publish:
            await repository.CreateCandidate(
                pb.CreateCandidateRequest(
                    request_id="viewer-denied",
                    expected_revision_id=0,
                    documents=[document("viewer", "1.0.0")],
                ),
                timeout=10,
            )
        assert publish.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_publish_identity_cannot_deprecate(live: LiveConfiguration) -> None:
    repository, _common, channel = live.stubs("ops")
    try:
        with pytest.raises(grpc.aio.AioRpcError) as deprecate:
            await repository.DeprecateConfigVersion(
                pb.DeprecateConfigVersionRequest(
                    request_id="ops-denied",
                    version_id="unknown",
                    reason="no",
                    expected_revision_id=0,
                ),
                timeout=10,
            )
        assert deprecate.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["viewer", "ops"])
async def test_non_admin_identities_cannot_backup(live: LiveConfiguration, identity: str) -> None:
    repository, _common, channel = live.stubs(identity)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as backup:
            await repository.CreateBackup(
                pb.CreateBackupRequest(request_id=f"{identity}-backup-denied"), timeout=10
            )
        assert backup.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_query_identity_cannot_read_self_configuration(
    live: LiveConfiguration,
) -> None:
    _repository, common, channel = live.stubs("viewer")
    try:
        with pytest.raises(grpc.aio.AioRpcError) as snapshot:
            await common.GetConfigSnapshot(
                common_pb2.GetConfigSnapshotRequest(scope=common_pb2.SNAPSHOT_SCOPE_DISK),
                timeout=10,
            )
        assert snapshot.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ["no-cn", "blank-cn"])
@pytest.mark.parametrize("interface", ["repository", "common"])
async def test_trusted_certificate_without_cn_is_unauthenticated(
    live: LiveConfiguration, interface: str, identity: str
) -> None:
    repository, common, channel = live.stubs(identity)
    try:
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            if interface == "repository":
                await repository.ListConfigs(pb.ListConfigsRequest(), timeout=10)
            else:
                await common.GetServiceStatus(common_pb2.Empty(), timeout=10)
        assert denied.value.code() == grpc.StatusCode.UNAUTHENTICATED
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("interface", ["repository", "common"])
async def test_unlisted_signed_identity_is_denied(live: LiveConfiguration, interface: str) -> None:
    repository, common, channel = live.stubs("stranger")
    try:
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            if interface == "repository":
                await repository.ListConfigs(pb.ListConfigsRequest(), timeout=10)
            else:
                await common.GetServiceStatus(common_pb2.Empty(), timeout=10)
        assert denied.value.code() == grpc.StatusCode.PERMISSION_DENIED
    finally:
        await channel.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ca", ["rogue-ca", "ca-without-client"])
async def test_wrong_ca_or_missing_client_certificate_fails_closed(
    live: LiveConfiguration, ca: str
) -> None:
    if ca == "ca-without-client":
        credentials = grpc.ssl_channel_credentials(
            root_certificates=(live.pki.tmp / "ca.crt").read_bytes()
        )
        channel = grpc.aio.secure_channel(
            f"127.0.0.1:{live.port}",
            credentials,
            options=(("grpc.ssl_target_name_override", "localhost"),),
        )
    else:
        channel = live.channel("rogue", ca="rogue-ca")
    try:
        stub = common_pb2_grpc.CommonServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await stub.GetServiceStatus(common_pb2.Empty(), timeout=10)
        assert denied.value.code() in {
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.UNAUTHENTICATED,
        }
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_plaintext_cannot_downgrade(live: LiveConfiguration) -> None:
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{live.port}")
    try:
        stub = common_pb2_grpc.CommonServiceStub(channel)
        with pytest.raises(grpc.aio.AioRpcError) as denied:
            await stub.GetServiceStatus(common_pb2.Empty(), timeout=10)
        assert denied.value.code() == grpc.StatusCode.UNAVAILABLE
    finally:
        await channel.close()
