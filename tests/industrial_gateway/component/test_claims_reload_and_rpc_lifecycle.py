"""ClaimTargets 租约在配置热重载下的失效边界，以及真实 grpc.aio deadline/取消传播。

ClaimTargets/lease_token 为 v1_9 设计书之外的生产扩展，行为独立钉住：
- Apply 变更某 server（监听器重建）→ 该 server 租约全部失效，旧 token 零修改；
- UNCHANGED server 与同批未变更 server 的租约保留；
- 并发 ACQUIRE 同一目标恰好一方成功；
- 客户端 deadline / cancel 真实传播，服务端保持一致可用。
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import grpc
import pytest
import yaml

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as pb
from contracts.scalar_codec import encode_scalar
from tests.support.gateway_v1 import (
    FEEDBACK_FLOAT,
    start_gateway_v1,
    two_server_specs,
)

pytestmark = [pytest.mark.component, pytest.mark.real_service]


def _update(variable_id: str, value: object) -> pb.VariableUpdate:
    return pb.VariableUpdate(variable_id=variable_id, value=encode_scalar(value))


@pytest.fixture
async def harness(tmp_path: Path):
    gateway = await start_gateway_v1(tmp_path / "root", two_server_specs())
    try:
        yield gateway
    finally:
        await gateway.stop()


async def _disk(stub) -> tuple[str, bytes, str]:
    reply = await stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK)
    )
    return reply.revision, reply.files[0].content, reply.files[0].relative_path


def _snapshot_file(relative_path: str, content: bytes) -> common.SnapshotFile:
    return common.SnapshotFile(
        relative_path=relative_path,
        content=content,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _content_with_gw1_stopped(content: bytes) -> bytes:
    document = yaml.safe_load(content)
    for server in document["servers"]:
        if server["server_id"] == "gw1":
            server["desired_state"] = "STOP"
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()


async def _apply_gw1_stop(harness) -> None:
    common_stub = harness.common_stub()
    revision, content, relative_path = await _disk(common_stub)
    candidate = await common_stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(
            files=[_snapshot_file(relative_path, _content_with_gw1_stopped(content))],
            expected_revision=revision,
        )
    )
    assert candidate.valid
    applied = await common_stub.ApplyConfigSnapshot(
        common.ApplyConfigSnapshotRequest(
            candidate_id=candidate.candidate_id,
            expected_revision=revision,
        )
    )
    assert not applied.restart_required
    statuses = {result.object_id: result.status for result in applied.apply_results}
    assert statuses["gw1"] == "APPLIED"
    assert statuses["gw2"] == "UNCHANGED"


async def test_apply_rebuild_invalidates_changed_server_claims_only(harness) -> None:
    stub = harness.gateway_stub()
    gw1 = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="A",
            variable_ids=[FEEDBACK_FLOAT],
            action="ACQUIRE",
            lease_ms=60000,
        )
    )
    gw2 = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw2",
            owner_id="A",
            variable_ids=[FEEDBACK_FLOAT],
            action="ACQUIRE",
            lease_ms=60000,
        )
    )
    assert gw1.accepted and gw2.accepted

    await _apply_gw1_stop(harness)

    # 变更 server：旧 token 被拒且变量零修改。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                lease_token=gw1.lease_token,
                values=[_update(FEEDBACK_FLOAT, 1.5)],
            )
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    read = await stub.ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_FLOAT])
    )
    assert read.values[0].quality == "UNKNOWN"
    # 租约表已清空：新 owner 可立即占用。
    takeover = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="B",
            variable_ids=[FEEDBACK_FLOAT],
            action="ACQUIRE",
            lease_ms=60000,
        )
    )
    assert takeover.accepted

    # 未变更 server：原租约保留，A 的 RENEW 仍成功。
    renewed = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw2",
            owner_id="A",
            variable_ids=[FEEDBACK_FLOAT],
            action="RENEW",
            lease_token=gw2.lease_token,
            lease_ms=60000,
        )
    )
    assert renewed.accepted


async def test_apply_identical_content_keeps_claims(harness) -> None:
    stub = harness.gateway_stub()
    common_stub = harness.common_stub()
    claim = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="A",
            variable_ids=[FEEDBACK_FLOAT],
            action="ACQUIRE",
            lease_ms=60000,
        )
    )
    revision, content, relative_path = await _disk(common_stub)
    candidate = await common_stub.PushConfigSnapshot(
        common.PushConfigSnapshotRequest(
            files=[_snapshot_file(relative_path, content)],
            expected_revision=revision,
        )
    )
    applied = await common_stub.ApplyConfigSnapshot(
        common.ApplyConfigSnapshotRequest(
            candidate_id=candidate.candidate_id,
            expected_revision=revision,
        )
    )
    assert {result.status for result in applied.apply_results} == {"UNCHANGED"}
    renewed = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="A",
            variable_ids=[FEEDBACK_FLOAT],
            action="RENEW",
            lease_token=claim.lease_token,
            lease_ms=60000,
        )
    )
    assert renewed.accepted


async def test_concurrent_acquire_same_target_single_winner(harness) -> None:
    stub = harness.gateway_stub()

    async def acquire(owner: str) -> bool:
        reply = await stub.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="gw1",
                owner_id=owner,
                variable_ids=[FEEDBACK_FLOAT],
                action="ACQUIRE",
                lease_ms=60000,
            )
        )
        return reply.accepted

    results = await asyncio.gather(*(acquire(f"owner-{i}") for i in range(8)))
    assert results.count(True) == 1
    assert results.count(False) == 7
    # 负方随后写入仍被占用方 fencing。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                values=[_update(FEEDBACK_FLOAT, 2.5)],
            )
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION


async def test_expired_deadline_is_mapped_and_server_stays_available(harness) -> None:
    stub = harness.gateway_stub()
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ListGateways(pb.ListGatewaysRequest(), timeout=-0.001)
    assert exc.value.code() is grpc.StatusCode.DEADLINE_EXCEEDED
    # 服务端未受影响：正常调用立即成功。
    listed = await stub.ListGateways(pb.ListGatewaysRequest())
    assert {item.gateway_id for item in listed.gateways} == {"gw1", "gw2"}


async def test_client_cancel_does_not_corrupt_service(harness) -> None:
    stub = harness.gateway_stub()

    async def call(index: int) -> bool:
        try:
            reply = await stub.ListGateways(pb.ListGatewaysRequest(page_size=1))
            return bool(reply.gateways)
        except asyncio.CancelledError:
            raise
        except grpc.aio.AioRpcError:
            return False

    tasks = [asyncio.create_task(call(i)) for i in range(120)]
    for index, task in enumerate(tasks):
        if index % 3 == 0:
            task.cancel()
    cancelled = 0
    succeeded = 0
    for task in tasks:
        try:
            if await task:
                succeeded += 1
        except asyncio.CancelledError:
            cancelled += 1
    assert cancelled == 40
    assert succeeded == 80
    # 取消风暴后数据面与配置面仍一致可用。
    listed = await stub.ListGateways(pb.ListGatewaysRequest())
    assert len(listed.gateways) == 2
