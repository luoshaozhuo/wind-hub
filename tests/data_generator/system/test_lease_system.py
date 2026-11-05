"""P1 v1_10 租约围栏专项系统复测（真实双进程 + 真实 Industrial Gateway）。

覆盖任务书 P1-01～P1-15。竞争类场景（P1-01/03/04）必须是两个独立
``data_generator.main`` 子进程连接同一真实 Gateway；协议权威类场景
（P1-08/12/13/15）使用真实 ``assemble_gateway`` 全栈 + 正式 Proto 原生客户端；
仅"客户端中途取消"这类真实环境无法稳定构造的场景使用冻结契约的
FaultGatewayService（P1-11/14），不断言 Mock 行为，只观测服务端注册表事实。

P1-02/P1-06 见 test_simulation_system.test_conflicting_target_ownership；
P1-07 见 test_lifecycle_system.test_crash_unknown_delivery（SIGKILL + TTL 回收）。
"""

from __future__ import annotations

import asyncio

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg
from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc

from ..conftest import dg_stubs, read_gateway_variable, ship_scenarios_document

pytestmark = [pytest.mark.system, pytest.mark.reliability]

ONE_FLOAT = {"GW01": [("pitch_angle", "float32", "feedback")]}
TWO_FLOAT = {
    "GW01": [
        ("pitch_angle", "float32", "feedback"),
        ("pitch_rate", "float32", "feedback"),
    ]
}
VAR_A = "GW01.pitch_angle"
VAR_B = "GW01.pitch_rate"


async def _gw_stub(bind: str):  # noqa: ANN202
    channel = grpc.aio.insecure_channel(bind)
    await channel.channel_ready()
    return channel, gw_rpc.IndustrialGatewayServiceStub(channel)


async def _claim(
    stub,  # noqa: ANN001
    gateway_id: str,
    owner: str,
    variables: list[str],
    *,
    action: str = "ACQUIRE",
    token: str = "",
    lease_ms: int = 10_000,
    timeout: float | None = None,
) -> gw.ClaimTargetsResponse:
    response: gw.ClaimTargetsResponse = await stub.ClaimTargets(
        gw.ClaimTargetsRequest(
            gateway_id=gateway_id,
            owner_id=owner,
            variable_ids=variables,
            action=action,
            lease_token=token,
            lease_ms=lease_ms,
        ),
        timeout=timeout,
    )
    return response


async def _write(
    stub,  # noqa: ANN001
    gateway_id: str,
    variable: str,
    value: float,
    token: str,
):  # noqa: ANN202
    return await stub.UpdateVariables(
        gw.UpdateVariablesRequest(
            gateway_id=gateway_id,
            lease_token=token,
            values=[
                gw.VariableUpdate(
                    variable_id=variable,
                    value=common.ScalarValue(double_value=value),
                )
            ],
        )
    )


async def _revision(common_stub) -> str:  # noqa: ANN001
    response = await common_stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    return str(response.revision)


async def _start(dg_stub, common_stub):  # noqa: ANN001, ANN202
    revision = await _revision(common_stub)
    return await dg_stub.StartGeneration(
        dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
    )


# --------------------------------------------------------------------------- #
# P1-01 同时申请：恰一胜，负方精确 RESOURCE_BUSY
# --------------------------------------------------------------------------- #


@pytest.mark.end_to_end
async def test_p1_01_simultaneous_claim_exactly_one_wins(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    doc = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=1.0,
        mappings=[("gw-a", VAR_A)],
    )
    bind_a, _proc_a = await dg_subprocess(configs_root(scenarios=doc))
    bind_b, _proc_b = await dg_subprocess(configs_root(scenarios=doc))
    channel_a, dg_a, common_a = await dg_stubs(bind_a)
    channel_b, dg_b, common_b = await dg_stubs(bind_b)
    try:
        # 两个独立进程同一时刻发起 StartGeneration（内部先 claim 后启动源）。
        first, second = await asyncio.gather(
            _start(dg_a, common_a),
            _start(dg_b, common_b),
        )
        winners = [response for response in (first, second) if response.accepted]
        losers = [response for response in (first, second) if not response.accepted]
        assert len(winners) == 1
        assert len(losers) == 1
        assert losers[0].error_code == "RESOURCE_BUSY", losers[0].error_code
    finally:
        await channel_a.close()
        await channel_b.close()


# --------------------------------------------------------------------------- #
# P1-03 不同变量：两实例共存，各自真实写入
# --------------------------------------------------------------------------- #


@pytest.mark.end_to_end
async def test_p1_03_disjoint_variables_coexist(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=TWO_FLOAT)
    doc_a = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=0.2,
        mappings=[("gw-a", VAR_A)],
    )
    doc_b = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=0.2,
        mappings=[("gw-a", VAR_B)],
    )
    bind_a, _proc_a = await dg_subprocess(configs_root(scenarios=doc_a))
    bind_b, _proc_b = await dg_subprocess(configs_root(scenarios=doc_b))
    channel_a, dg_a, common_a = await dg_stubs(bind_a)
    channel_b, dg_b, common_b = await dg_stubs(bind_b)
    try:
        started_a = await _start(dg_a, common_a)
        started_b = await _start(dg_b, common_b)
        assert started_a.accepted, started_a.message
        assert started_b.accepted, started_b.message
        await asyncio.sleep(1.0)
        sample_a = await read_gateway_variable(gateway.data_bind, "gw-a", VAR_A)
        sample_b = await read_gateway_variable(gateway.data_bind, "gw-a", VAR_B)
        assert sample_a.quality == "GOOD" and sample_b.quality == "GOOD"
        # 两个实例持续推进且互不覆盖。
        a1, b1 = sample_a.value.double_value, sample_b.value.double_value
        await asyncio.sleep(0.6)
        a2 = (await read_gateway_variable(gateway.data_bind, "gw-a", VAR_A)).value.double_value
        b2 = (await read_gateway_variable(gateway.data_bind, "gw-a", VAR_B)).value.double_value
        assert {a1, a2} != {b1, b2} or (a2 != a1 and b2 != b1)
        for stub, gen in ((dg_a, started_a.generation_id), (dg_b, started_b.generation_id)):
            stopped = await stub.StopGeneration(
                dg.StopGenerationRequest(generation_id=gen, expected_revision="")
            )
            assert stopped.accepted
    finally:
        await channel_a.close()
        await channel_b.close()


# --------------------------------------------------------------------------- #
# P1-04 多变量整组拒绝：一个被占 => 整组拒绝，无部分占用残留
# --------------------------------------------------------------------------- #


@pytest.mark.end_to_end
async def test_p1_04_group_rejected_without_partial_occupancy(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=TWO_FLOAT)
    doc_a = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=1.0,
        mappings=[("gw-a", VAR_A)],
    )
    doc_b = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=1.0,
        mappings=[("gw-a", VAR_A), ("gw-a", VAR_B)],
    )
    bind_a, _proc_a = await dg_subprocess(configs_root(scenarios=doc_a))
    bind_b, _proc_b = await dg_subprocess(configs_root(scenarios=doc_b))
    channel_a, dg_a, common_a = await dg_stubs(bind_a)
    channel_b, dg_b, common_b = await dg_stubs(bind_b)
    raw_channel, raw = await _gw_stub(gateway.data_bind)
    try:
        started_a = await _start(dg_a, common_a)
        assert started_a.accepted, started_a.message
        rejected = await _start(dg_b, common_b)
        assert not rejected.accepted
        assert rejected.error_code == "RESOURCE_BUSY", rejected.error_code
        # 整组原子：被拒方一个变量都不得留下占用。VAR_A 仍忙、VAR_B 必须立即可得。
        busy = await _claim(raw, "gw-a", "probe", [VAR_A])
        # v1 租约词表：目标已被占用返回 TARGET_CLAIMED（整组原子拒绝语义不变）。
        assert not busy.accepted and busy.error_code == "TARGET_CLAIMED"
        free = await _claim(raw, "gw-a", "probe", [VAR_B])
        assert free.accepted, free.error_code
        await _claim(raw, "gw-a", "probe", [VAR_B], action="RELEASE", token=free.lease_token)
    finally:
        await raw_channel.close()
        await channel_a.close()
        await channel_b.close()


# --------------------------------------------------------------------------- #
# P1-05 持续生成期间 RENEW 真实发生（同一 token、合法 lease_ms、按 1/3 租期）
# --------------------------------------------------------------------------- #


async def test_p1_05_renewal_happens_during_generation(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    fault = await start_fault_gateway(
        {"gw-a": [(VAR_A, "float32", True)]},
        {"gw-a": {"delay": 0.005}},
    )
    # timeout=2.0 => lease_ms=12000ms，续租周期 4s；观察 9s 应见 ≥2 次 RENEW。
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.2,
            mappings=[("gw-a", VAR_A)],
        ),
        output_timeout=2.0,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        started = await _start(dg_stub, common_stub)
        assert started.accepted, started.message
        await asyncio.sleep(9.0)
        calls = fault.service.claim_calls["gw-a"]
        acquires = [call for call in calls if call["action"] == "ACQUIRE"]
        renews = [call for call in calls if call["action"] == "RENEW"]
        assert len(acquires) == 1
        assert len(renews) >= 2, [call["action"] for call in calls]
        token = acquires[0]["response_token"]
        assert token and all(call["token"] == token for call in renews)
        assert all(call["owner_id"] == started.generation_id for call in calls)
        assert 10_000 <= acquires[0]["lease_ms"] <= 60_000
    finally:
        await channel.close()


# --------------------------------------------------------------------------- #
# P1-08 旧 token fencing：A 到期、B 取得后，A 旧 token 写入被权威拒绝
# --------------------------------------------------------------------------- #


async def test_p1_08_expired_token_write_fenced_by_gateway(start_real_gateway) -> None:
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    channel, stub = await _gw_stub(gateway.data_bind)
    try:
        lease_a = await _claim(stub, "gw-a", "owner-a", [VAR_A], lease_ms=1000)
        assert lease_a.accepted and lease_a.lease_token
        await asyncio.sleep(1.1)  # 超过服务端 TTL（下限 1000ms）
        lease_b = await _claim(stub, "gw-a", "owner-b", [VAR_A], lease_ms=10_000)
        assert lease_b.accepted and lease_b.lease_token != lease_a.lease_token
        with pytest.raises(grpc.RpcError) as fenced:
            await _write(stub, "gw-a", VAR_A, 1.25, lease_a.lease_token)
        assert fenced.value.code() == grpc.StatusCode.FAILED_PRECONDITION
        await _write(stub, "gw-a", VAR_A, 7.5, lease_b.lease_token)
        sample = await read_gateway_variable(gateway.data_bind, "gw-a", VAR_A)
        assert sample.value.double_value == pytest.approx(7.5)
    finally:
        await channel.close()


# --------------------------------------------------------------------------- #
# P1-09 Gateway 重启：内存租约全失效；DG 续租/写入探测判死，Generation FAILED
# --------------------------------------------------------------------------- #


@pytest.mark.network_failure
async def test_p1_09_gateway_restart_invalidates_all_tokens(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=TWO_FLOAT)
    channel_raw, raw = await _gw_stub(gateway.data_bind)
    # 旁路原生客户端持有的租约在重启后同样必须失效。
    side = await _claim(raw, "gw-a", "side", [VAR_B], lease_ms=60_000)
    assert side.accepted
    doc = ship_scenarios_document(
        {"gw-a": gateway.data_bind},
        interval=0.15,
        mappings=[("gw-a", VAR_A)],
    )
    service_path = configs_root(scenarios=doc)
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        started = await _start(dg_stub, common_stub)
        assert started.accepted, started.message
        await read_gateway_variable(gateway.data_bind, "gw-a", VAR_A)
        await asyncio.sleep(0.4)

        await gateway.stop()
        gateway2 = await start_real_gateway(
            endpoint_id="gw-a", devices=TWO_FLOAT, data_bind=gateway.data_bind
        )
        try:
            with pytest.raises(grpc.RpcError) as side_fenced:
                await _write(raw, "gw-a", VAR_B, 9.9, side.lease_token)
            assert side_fenced.value.code() == grpc.StatusCode.FAILED_PRECONDITION

            deadline = asyncio.get_running_loop().time() + 10.0
            state = ""
            while asyncio.get_running_loop().time() < deadline:
                response = await dg_stub.GetGeneration(common.ObjectId(id=started.generation_id))
                generation = response.generation
                state = generation.actual_state
                if state == "FAILED":
                    assert "lease" in generation.last_error.lower()
                    break
                await asyncio.sleep(0.15)
            assert state == "FAILED", state
        finally:
            await gateway2.stop()
    finally:
        await channel.close()
        await channel_raw.close()


# --------------------------------------------------------------------------- #
# P1-10 双 Gateway：第二组申请失败 => 第一组已确认租约真实 RELEASE 回滚
# --------------------------------------------------------------------------- #


async def test_p1_10_second_group_failure_rolls_back_first(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    fault_a = await start_fault_gateway({"gw-a": [(VAR_A, "float32", True)]})
    fault_b = await start_fault_gateway(
        {"gw-b": [("GW01.rpm", "float32", True)]},
        {"gw-b": {"claim_abort": {"ACQUIRE": grpc.StatusCode.UNAVAILABLE}}},
    )
    doc = ship_scenarios_document(
        {"gw-a": fault_a.bind, "gw-b": fault_b.bind},
        interval=1.0,
        mappings=[("gw-a", VAR_A), ("gw-b", "GW01.rpm")],
    )
    bind, _process = await dg_subprocess(configs_root(scenarios=doc, output_timeout=1.0))
    channel, dg_stub, common_stub = await dg_stubs(bind)
    raw_channel, raw = await _gw_stub(fault_a.bind)
    try:
        rejected = await _start(dg_stub, common_stub)
        assert not rejected.accepted
        assert rejected.error_code == "GATEWAY_UNAVAILABLE", rejected.error_code
        actions_a = [call["action"] for call in fault_a.service.claim_calls["gw-a"]]
        assert actions_a == ["ACQUIRE", "RELEASE"], actions_a
        # 回滚是真实的服务端 RELEASE：第三方立即可取得 gw-a，无需等 TTL。
        probe = await _claim(raw, "gw-a", "probe", [VAR_A])
        assert probe.accepted, probe.error_code
    finally:
        await raw_channel.close()
        await channel.close()


# --------------------------------------------------------------------------- #
# P1-11 Claim 超时：UNKNOWN 不谎报得失；服务端结果悬置时不伪造 RELEASE
# P1-14 客户端取消 ClaimTargets：取消后无遗留占用
# --------------------------------------------------------------------------- #


async def test_p1_11_14_claim_timeout_and_cancel_leaves_no_residue(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    gate = asyncio.Event()
    fault = await start_fault_gateway(
        {"gw-a": [(VAR_A, "float32", True)]},
        {"gw-a": {"claim_gate": gate}},
    )
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=1.0,
            mappings=[("gw-a", VAR_A)],
        ),
        output_timeout=0.5,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    raw_channel, raw = await _gw_stub(fault.bind)
    try:
        rejected = await _start(dg_stub, common_stub)
        assert not rejected.accepted
        assert rejected.error_code == "GATEWAY_UNAVAILABLE", rejected.error_code
        # 申请结果未知：DG 不得伪造 RELEASE（悬置调用由服务端取消语义终结）。
        actions = [call["action"] for call in fault.service.claim_calls["gw-a"]]
        assert actions == ["ACQUIRE"], actions

        # 放行挂起的服务端处理；被客户端取消的 Claim 绝不能事后落地为占用。
        gate.set()
        await asyncio.sleep(0.3)
        probe = await _claim(raw, "gw-a", "probe", [VAR_A])
        assert probe.accepted, f"cancelled claim left residue: {probe.error_code}"
        await _claim(raw, "gw-a", "probe", [VAR_A], action="RELEASE", token=probe.lease_token)
    finally:
        await raw_channel.close()
        await channel.close()


# --------------------------------------------------------------------------- #
# P1-12 写入与租约转移无交错：转移后旧 token 写入全部被围栏拒绝，存储不回退
# --------------------------------------------------------------------------- #


async def test_p1_12_transfer_has_no_interleaved_stale_write(start_real_gateway) -> None:
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    channel, stub = await _gw_stub(gateway.data_bind)
    try:
        for _round in range(5):
            lease_a = await _claim(stub, "gw-a", "owner-a", [VAR_A], lease_ms=1000)
            assert lease_a.accepted
            await _write(stub, "gw-a", VAR_A, 1.1, lease_a.lease_token)
            await asyncio.sleep(1.05)  # A 租约到期
            lease_b = await _claim(stub, "gw-a", "owner-b", [VAR_A], lease_ms=10_000)
            assert lease_b.accepted

            stale_rejections = 0

            async def stale_write(token: str = lease_a.lease_token) -> None:
                nonlocal stale_rejections
                try:
                    await _write(stub, "gw-a", VAR_A, 1.1, token)
                except grpc.RpcError as exc:
                    if exc.code() == grpc.StatusCode.FAILED_PRECONDITION:
                        stale_rejections += 1
                    else:
                        raise

            async def fresh_write(value: float, token: str = lease_b.lease_token) -> None:
                await _write(stub, "gw-a", VAR_A, value, token)

            await asyncio.gather(
                *[stale_write() for _ in range(8)],
                *[fresh_write(7.0 + _round) for _ in range(8)],
            )
            assert stale_rejections == 8
            stored = await read_gateway_variable(gateway.data_bind, "gw-a", VAR_A)
            assert stored.value.double_value == pytest.approx(7.0 + _round)
            await _claim(
                stub,
                "gw-a",
                "owner-b",
                [VAR_A],
                action="RELEASE",
                token=lease_b.lease_token,
            )
    finally:
        await channel.close()


# --------------------------------------------------------------------------- #
# P1-13 并发抢占/释放风暴：唯一所有权、无死锁、无双重持有
# --------------------------------------------------------------------------- #


async def test_p1_13_concurrent_claim_storm_single_owner(start_real_gateway) -> None:
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    channel, stub = await _gw_stub(gateway.data_bind)
    try:
        for cycle in range(4):

            async def attempt(index: int, cycle: int = cycle) -> gw.ClaimTargetsResponse:
                return await _claim(
                    stub, "gw-a", f"owner-{cycle}-{index}", [VAR_A], lease_ms=10_000
                )

            replies = await asyncio.gather(*[attempt(i) for i in range(25)])
            winners = [reply for reply in replies if reply.accepted]
            assert len(winners) == 1
            winner = winners[0]
            # 占用期间任何新申请精确失败为 TARGET_CLAIMED，无双重所有权。
            busy = await _claim(stub, "gw-a", f"outsider-{cycle}", [VAR_A])
            assert not busy.accepted and busy.error_code == "TARGET_CLAIMED"
            # 无 token 不得续租；赢家显式 RELEASE 后下一轮必须立即可得（无死锁）。
            bogus_renew = await _claim(stub, "gw-a", f"owner-{cycle}-0", [VAR_A], action="RENEW")
            assert not bogus_renew.accepted
            released = await _claim(
                stub,
                "gw-a",
                # owner 必须与申请时一致才能释放（服务端全等校验）。
                next(f"owner-{cycle}-{i}" for i, reply in enumerate(replies) if reply.accepted),
                [VAR_A],
                action="RELEASE",
                token=winner.lease_token,
            )
            assert released.accepted, released.error_code
    finally:
        await channel.close()


# --------------------------------------------------------------------------- #
# P1-15 持旧 token 伪造 owner_id 续租：服务端 LEASE_LOST 拒绝，真 owner 不受影响
# --------------------------------------------------------------------------- #


async def test_p1_15_forged_owner_renewal_rejected(start_real_gateway) -> None:
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    channel, stub = await _gw_stub(gateway.data_bind)
    try:
        lease = await _claim(stub, "gw-a", "owner-a", [VAR_A], lease_ms=10_000)
        assert lease.accepted
        forged = await _claim(
            stub,
            "gw-a",
            "forger",
            [VAR_A],
            action="RENEW",
            token=lease.lease_token,
            lease_ms=10_000,
        )
        assert not forged.accepted
        assert forged.error_code == "LEASE_LOST", forged.error_code
        # 伪造续租是只读失败：真 owner 的租约不被改动。
        genuine = await _claim(
            stub,
            "gw-a",
            "owner-a",
            [VAR_A],
            action="RENEW",
            token=lease.lease_token,
            lease_ms=10_000,
        )
        assert genuine.accepted
        await _write(stub, "gw-a", VAR_A, 4.25, lease.lease_token)
        sample = await read_gateway_variable(gateway.data_bind, "gw-a", VAR_A)
        assert sample.value.double_value == pytest.approx(4.25)
    finally:
        await channel.close()
