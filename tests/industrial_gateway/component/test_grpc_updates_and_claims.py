"""UpdateVariables / ClaimTargets 真实 grpc.aio 契约（8 RPC 中的注入面）。

ClaimTargets/lease_token 为 v1_9 设计书之外的生产扩展（与 Data Generator 联调
引入），本文件独立钉住其行为，验收报告中标记为待同步设计版本差异。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import grpc
import pytest

from contracts import industrial_gateway_pb2 as pb
from core.infrastructure.rpc.scalar_codec import encode_scalar
from tests.support.gateway_v1 import (
    FEEDBACK_COIL_0,
    FEEDBACK_FLOAT,
    FEEDBACK_PAIR,
    FEEDBACK_UINT,
    POINT_TABLE,
    start_gateway_v1,
    two_server_specs,
)

pytestmark = [pytest.mark.component, pytest.mark.real_service]


def _update(variable_id: str, value: object) -> pb.VariableUpdate:
    return pb.VariableUpdate(variable_id=variable_id, value=encode_scalar(value))


@pytest.fixture
async def harness(tmp_path: Path):
    gateway = await start_gateway_v1(tmp_path / "root", two_server_specs(control_enabled=False))
    try:
        yield gateway
    finally:
        await gateway.stop()


async def test_update_per_item_results_and_readback(harness) -> None:
    stub = harness.gateway_stub()
    reply = await stub.UpdateVariables(
        pb.UpdateVariablesRequest(
            gateway_id="gw1",
            values=[
                _update(FEEDBACK_UINT, 4242),
                _update(FEEDBACK_FLOAT, 8.5),
                _update("missing_point", 1),
                _update("cmd_uint2", 70000),  # 超 uint16
                pb.VariableUpdate(variable_id=FEEDBACK_PAIR),  # 缺 scalar
            ],
        )
    )
    assert [(r.variable_id, r.success, r.error_code) for r in reply.results] == [
        (FEEDBACK_UINT, True, ""),
        (FEEDBACK_FLOAT, True, ""),
        ("missing_point", False, "NOT_FOUND"),
        ("cmd_uint2", False, "INVALID_VALUE"),
        (FEEDBACK_PAIR, False, "INVALID_VALUE"),
    ]
    # 成功项保留，失败项未生效。
    read = await stub.ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_UINT, FEEDBACK_FLOAT])
    )
    values = {sample.point_id: sample.value for sample in read.values}
    # 通用标量编码器对 Python int 统一走 sint64 线格式。
    assert values[FEEDBACK_UINT].WhichOneof("kind") == "sint64_value"
    assert values[FEEDBACK_UINT].sint64_value == 4242
    assert abs(values[FEEDBACK_FLOAT].double_value - 8.5) < 1e-6


async def test_update_unknown_gateway_not_found(harness) -> None:
    stub = harness.gateway_stub()
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="ghost",
                values=[_update(FEEDBACK_UINT, 1)],
            )
        )
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


async def test_duplicate_variables_reject_whole_batch_before_mutation(harness) -> None:
    stub = harness.gateway_stub()
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                values=[_update(FEEDBACK_UINT, 1), _update(FEEDBACK_UINT, 2)],
            )
        )
    assert exc.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    assert "duplicate" in exc.value.details()
    read = await stub.ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_UINT])
    )
    assert read.values[0].quality == "UNKNOWN"  # 无任何写入生效


async def test_full_point_set_batch_succeeds(harness) -> None:
    """点表全部 15 个变量可同批写入（当前点表规模下的满批边界）。"""
    stub = harness.gateway_stub()
    values = {
        FEEDBACK_FLOAT: 1.0,
        FEEDBACK_PAIR: 1,
        FEEDBACK_UINT: 1,
        FEEDBACK_COIL_0: True,
        "fb_coil_1": True,
        "fb_coil_15": True,
        "fb_coil_16": True,
        "fb_coil_17": True,
        "fb_discrete": True,
        "fb_input": 1,
        "cmd_float": 1.0,
        "cmd_uint2": 1,
        "cmd_uint": 1,
        "cmd_coil": True,
        "cmd_coil_b": True,
    }
    assert set(values) == set(POINT_TABLE)
    reply = await stub.UpdateVariables(
        pb.UpdateVariablesRequest(
            gateway_id="gw1",
            values=[_update(name, value) for name, value in values.items()],
        )
    )
    failed = [r for r in reply.results if not r.success]
    assert failed == [], failed


async def test_claim_acquire_protects_targets(harness) -> None:
    stub = harness.gateway_stub()
    claim = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="generator-A",
            variable_ids=[FEEDBACK_FLOAT],
            action="ACQUIRE",
            lease_ms=10000,
        )
    )
    assert claim.accepted and claim.lease_token

    # 第二写入方无 token：不能覆盖被占用目标。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                values=[_update(FEEDBACK_FLOAT, 9.0)],
            )
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    read = await stub.ReadVariables(
        pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_FLOAT])
    )
    assert read.values[0].quality == "UNKNOWN"  # 被拒绝的写入未造成任何修改

    # 持有者用 token 写入成功。
    ok = await stub.UpdateVariables(
        pb.UpdateVariablesRequest(
            gateway_id="gw1",
            lease_token=claim.lease_token,
            values=[_update(FEEDBACK_FLOAT, 5.0)],
        )
    )
    assert all(result.success for result in ok.results)

    # 其他写入方即使伪造同 owner 的错误 token 也被拒。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                lease_token="forged-token",
                values=[_update(FEEDBACK_FLOAT, 6.0)],
            )
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION


async def test_claim_conflict_and_invalid_requests(harness) -> None:
    stub = harness.gateway_stub()
    first = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="A",
            variable_ids=[FEEDBACK_FLOAT, FEEDBACK_PAIR],
            action="ACQUIRE",
            lease_ms=10000,
        )
    )
    assert first.accepted
    conflict = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="B",
            variable_ids=[FEEDBACK_PAIR],
            action="ACQUIRE",
            lease_ms=10000,
        )
    )
    assert not conflict.accepted and conflict.error_code == "TARGET_CLAIMED"

    bad_target = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="B",
            variable_ids=["not_a_point"],
            action="ACQUIRE",
            lease_ms=10000,
        )
    )
    assert bad_target.error_code == "INVALID_TARGET"

    too_short = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="B",
            variable_ids=[FEEDBACK_UINT],
            action="ACQUIRE",
            lease_ms=999,
        )
    )
    assert too_short.error_code == "INVALID_LEASE_MS"

    duplicated = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="B",
            variable_ids=[FEEDBACK_UINT, FEEDBACK_UINT],
            action="ACQUIRE",
            lease_ms=10000,
        )
    )
    assert duplicated.error_code == "INVALID_CLAIM"

    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ClaimTargets(
            pb.ClaimTargetsRequest(
                gateway_id="ghost",
                owner_id="B",
                variable_ids=[FEEDBACK_UINT],
                action="ACQUIRE",
                lease_ms=10000,
            )
        )
    assert exc.value.code() is grpc.StatusCode.NOT_FOUND


async def test_renew_release_allows_takeover(harness) -> None:
    stub = harness.gateway_stub()
    claim = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="A",
            variable_ids=[FEEDBACK_COIL_0],
            action="ACQUIRE",
            lease_ms=10000,
        )
    )
    renewed = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="A",
            variable_ids=[FEEDBACK_COIL_0],
            action="RENEW",
            lease_token=claim.lease_token,
            lease_ms=10000,
        )
    )
    assert renewed.accepted and renewed.lease_token == claim.lease_token
    # RENEW 必须精确匹配原目标集合，子集不可。
    partial = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="A",
            variable_ids=[],
            action="RENEW",
            lease_token=claim.lease_token,
            lease_ms=10000,
        )
    )
    assert partial.error_code == "INVALID_CLAIM"

    released = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="A",
            variable_ids=[FEEDBACK_COIL_0],
            action="RELEASE",
            lease_token=claim.lease_token,
        )
    )
    assert released.accepted
    taken = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="B",
            variable_ids=[FEEDBACK_COIL_0],
            action="ACQUIRE",
            lease_ms=10000,
        )
    )
    assert taken.accepted
    # 旧 token 已失效：持有者 A 不能再写。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                lease_token=claim.lease_token,
                values=[_update(FEEDBACK_COIL_0, True)],
            )
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert (
        await stub.ReadVariables(
            pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_COIL_0])
        )
    ).values[0].quality == "UNKNOWN"


async def test_expired_token_cannot_modify_after_takeover(harness) -> None:
    """关键安全语义：过期 → 被接管后，旧 token 写入必须失败且变量不变。"""
    stub = harness.gateway_stub()
    old = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="A",
            variable_ids=[FEEDBACK_PAIR],
            action="ACQUIRE",
            lease_ms=1000,
        )
    )
    assert old.accepted
    await asyncio.sleep(1.05)
    new = await stub.ClaimTargets(
        pb.ClaimTargetsRequest(
            gateway_id="gw1",
            owner_id="B",
            variable_ids=[FEEDBACK_PAIR],
            action="ACQUIRE",
            lease_ms=10000,
        )
    )
    assert new.accepted and new.lease_token != old.lease_token
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.UpdateVariables(
            pb.UpdateVariablesRequest(
                gateway_id="gw1",
                lease_token=old.lease_token,
                values=[_update(FEEDBACK_PAIR, 11)],
            )
        )
    assert exc.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert (
        await stub.ReadVariables(
            pb.ReadVariablesRequest(gateway_id="gw1", variable_ids=[FEEDBACK_PAIR])
        )
    ).values[0].quality == "UNKNOWN"
    # 新持有者写入成功。
    ok = await stub.UpdateVariables(
        pb.UpdateVariablesRequest(
            gateway_id="gw1",
            lease_token=new.lease_token,
            values=[_update(FEEDBACK_PAIR, 22)],
        )
    )
    assert ok.results[0].success
