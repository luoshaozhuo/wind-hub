"""UC-07 System：独立进程上 15 RPC 契约操作、错误映射、超时取消与关闭语义。

覆盖 §14.2 标识：
test_contract_operations / test_contract_errors / test_timeout_cancel /
test_shutdown_inflight / test_concurrent_start_atomic_target_reservation /
test_stop_retains_target_until_io_settled。
"""

from __future__ import annotations

import asyncio
import time

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import data_generator_pb2 as dg

from ..conftest import dg_stubs, metric_map, ship_scenarios_document

pytestmark = [pytest.mark.system, pytest.mark.reliability]

VARIABLE = "GW01.pitch_angle"
ONE_FLOAT = {"GW01": [("pitch_angle", "float32", "feedback")]}


async def _revision(common_stub) -> str:  # noqa: ANN001
    response = await common_stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    return str(response.revision)


async def test_contract_operations(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-07-S01：独立进程上 15 个 RPC 基本成功路径真实可调。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=3600.0,
            mappings=[("gw-a", VARIABLE)],
        )
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        info = await common_stub.GetServiceInfo(common.Empty())
        assert info.service_id == "data_generator"
        status = await common_stub.GetServiceStatus(common.Empty())
        assert status.health in ("HEALTHY", "DEGRADED")
        capabilities = await common_stub.GetCapabilities(common.Empty())
        assert len(capabilities.rpc_names) == 15
        devices = await dg_stub.ListDevices(dg.ListDevicesRequest())
        assert {item.device_id for item in devices.devices} == {"GW01"}
        assert await dg_stub.GetDevice(common.ObjectId(id="GW01"))
        scenarios = await dg_stub.ListScenarios(dg.ListScenariosRequest())
        assert scenarios.scenarios[0].scenario_id == "demo-random"
        for scope in (
            common.SNAPSHOT_SCOPE_DISK,
            common.SNAPSHOT_SCOPE_RUNTIME,
        ):
            snapshot = await common_stub.GetConfigSnapshot(
                common.GetConfigSnapshotRequest(scope=scope)
            )
            assert snapshot.files and snapshot.revision
        metrics = await common_stub.GetMetrics(common.GetMetricsRequest())
        assert metrics.metrics is not None
        events_response = await common_stub.GetEvents(common.GetEventsRequest())
        assert events_response is not None
        diagnose = await common_stub.Diagnose(common.DiagnoseRequest(target_id="ghost"))
        assert diagnose.overall_status == "UNKNOWN"

        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        fetched = await dg_stub.GetGeneration(common.ObjectId(id=started.generation_id))
        assert fetched.generation.scenario_id == "demo-random"
        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(
                generation_id=started.generation_id, expected_revision=revision
            )
        )
        assert stopped.accepted
    finally:
        await channel.close()


@pytest.mark.safety
@pytest.mark.invalid_input
async def test_contract_errors(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-07-S02：错误码准确，拒绝不产生副作用。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=3600.0,
            mappings=[("gw-a", VARIABLE)],
        )
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        with pytest.raises(grpc.RpcError) as missing_device:
            await dg_stub.GetDevice(common.ObjectId(id="nope"))
        assert missing_device.value.code() == grpc.StatusCode.NOT_FOUND
        with pytest.raises(grpc.RpcError) as missing_generation:
            await dg_stub.GetGeneration(common.ObjectId(id="g-nope"))
        assert missing_generation.value.code() == grpc.StatusCode.NOT_FOUND
        with pytest.raises(grpc.RpcError) as bad_token:
            await dg_stub.ListDevices(dg.ListDevicesRequest(page_token="zzz"))
        assert bad_token.value.code() == grpc.StatusCode.INVALID_ARGUMENT
        with pytest.raises(grpc.RpcError) as bad_scope:
            await common_stub.GetMetrics(common.GetMetricsRequest(scope="galaxy:x"))
        assert bad_scope.value.code() == grpc.StatusCode.INVALID_ARGUMENT

        # 修订号冲突在响应体内返回，且没有任何 Generation 被创建。
        revision = await _revision(common_stub)
        conflict = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision="stale-rev")
        )
        assert conflict.accepted is False
        assert conflict.error_code == "REVISION_CONFLICT"

        # 拒绝不得留下部分占用：正确修订号可立即取得目标并正常停止。
        accepted = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert accepted.accepted, accepted.message
        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(
                generation_id=accepted.generation_id, expected_revision=revision
            )
        )
        assert stopped.accepted
    finally:
        await channel.close()


@pytest.mark.deadline
async def test_timeout_cancel(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-07-S03：逻辑超时按 UNKNOWN 隔离，不盲重试，不把超时当底层 I/O 完成。"""
    gate = asyncio.Event()
    catalog = {"gw-a": [(VARIABLE, "float32", True)]}
    fault = await start_fault_gateway(catalog, {"gw-a": {"gate": gate}})
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.1,
            mappings=[("gw-a", VARIABLE)],
        ),
        output_timeout=0.3,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        # 等首批原生调用真实发出（其原生硬截止为 timeout*2=0.6s）。
        sent_at = asyncio.get_running_loop().time()
        deadline = sent_at + 5.0
        while (
            not fault.service.update_calls["gw-a"] and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.02)
        assert fault.service.update_calls["gw-a"], "no native UpdateVariables call observed"
        # 逻辑超时（0.3s）后、原生硬截止（0.6s）前：仅一批底层调用，Gateway 被隔离。
        await asyncio.sleep(0.42)
        assert len(fault.service.update_calls["gw-a"]) == 1
        metrics = metric_map(
            await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=30_000))
        )
        assert metrics["data_generator.unknown_total"] >= 1
        status = await common_stub.GetServiceStatus(common.Empty())
        assert status.health == "DEGRADED"

        # 在原生硬截止（0.6s）内让挂起调用成功返回：隔离解除，恢复 HEALTHY；
        # 曾超时的批次只计 UNKNOWN，不得被谎报为确认。
        gate.set()
        healthy_deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < healthy_deadline:
            if (await common_stub.GetServiceStatus(common.Empty())).health == "HEALTHY":
                break
            await asyncio.sleep(0.1)
        assert (await common_stub.GetServiceStatus(common.Empty())).health == "HEALTHY"
        metrics = metric_map(
            await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=30_000))
        )
        assert metrics["data_generator.unknown_total"] >= 1
        # 停止必须等原生 I/O 可信收敛后才释放目标（收敛可能滞后一个结算窗口，故轮询）。
        stopped = None
        stop_deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < stop_deadline:
            stopped = await dg_stub.StopGeneration(
                dg.StopGenerationRequest(
                    generation_id=started.generation_id, expected_revision=revision
                )
            )
            if stopped.actual_state == "STOPPED":
                break
            await asyncio.sleep(0.1)
        assert stopped is not None and stopped.accepted
        assert stopped.actual_state == "STOPPED", stopped
    finally:
        await channel.close()


@pytest.mark.restart
async def test_shutdown_inflight(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-07-S03：关闭期间拒绝新请求，已有在途批次安全收敛，进程有界退出。"""
    catalog = {"gw-a": [(VARIABLE, "float32", True)]}
    fault = await start_fault_gateway(catalog, {"gw-a": {"delay": 0.05}})
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.05,
            mappings=[("gw-a", VARIABLE)],
        ),
        output_timeout=1.0,
    )
    bind, process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        await asyncio.sleep(0.5)
        assert len(fault.service.update_calls["gw-a"]) >= 3
    finally:
        await channel.close()

    began = time.monotonic()
    process.terminate()
    # 关闭窗口内新连接/新 RPC 必须被拒（drain 期间监听器停止接收）。
    rejected_after_shutdown = False
    deadline = asyncio.get_running_loop().time() + 10.0
    while asyncio.get_running_loop().time() < deadline and process.poll() is None:
        try:
            async with grpc.aio.insecure_channel(bind) as late_channel:
                await asyncio.wait_for(late_channel.channel_ready(), timeout=0.5)
                late_stub = common_rpc.CommonServiceStub(late_channel)
                await late_stub.GetServiceInfo(common.Empty(), timeout=0.5)
        except grpc.RpcError as error:
            if error.code() in (
                grpc.StatusCode.UNAVAILABLE,
                grpc.StatusCode.CANCELLED,
                grpc.StatusCode.DEADLINE_EXCEEDED,
            ):
                rejected_after_shutdown = True
                break
        except TimeoutError:
            rejected_after_shutdown = True
            break
        await asyncio.sleep(0.05)
    process.wait(timeout=20)
    elapsed = time.monotonic() - began
    assert rejected_after_shutdown, "shutdown window accepted a new RPC"
    assert process.returncode == 0
    # 在途原生调用 ~50ms 收敛 + grace 3s，远小于未收敛上限（timeout*2+grace≈5s）。
    assert elapsed < 12.0, elapsed


@pytest.mark.concurrency
async def test_concurrent_start_atomic_target_reservation(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-07-S02：同一目标并发 Start 只能一个成功，失败者不留占用。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=3600.0,
            mappings=[("gw-a", VARIABLE)],
        )
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        responses = await asyncio.gather(
            *[
                dg_stub.StartGeneration(
                    dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
                )
                for _ in range(2)
            ]
        )
        accepted = [response for response in responses if response.accepted]
        rejected = [response for response in responses if not response.accepted]
        assert len(accepted) == 1
        assert len(rejected) == 1
        assert rejected[0].error_code == "RESOURCE_BUSY"

        # 失败方没有留下部分占用：停止赢家后可立即再启动。
        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(
                generation_id=accepted[0].generation_id, expected_revision=revision
            )
        )
        assert stopped.accepted
        again = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert again.accepted, again.message
    finally:
        await channel.close()


@pytest.mark.safety
@pytest.mark.deadline
async def test_stop_retains_target_until_io_settled(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-07-S03：Stop 超时后未收敛目标不得被新 Generation 抢占，收敛后方可取得。"""
    gate = asyncio.Event()
    catalog = {"gw-a": [(VARIABLE, "float32", True)]}
    fault = await start_fault_gateway(catalog, {"gw-a": {"gate": gate}})
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.1,
            mappings=[("gw-a", VARIABLE)],
        ),
        output_timeout=1.0,  # 逻辑超时 1.0s，原生硬截止 2.0s
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        first = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert first.accepted, first.message
        # 等首批真实发出并越过逻辑超时（DEGRADED 即隔离证据），原生调用仍挂起。
        degraded_deadline = asyncio.get_running_loop().time() + 8.0
        while asyncio.get_running_loop().time() < degraded_deadline:
            if (await common_stub.GetServiceStatus(common.Empty())).health == "DEGRADED":
                break
            await asyncio.sleep(0.05)
        assert (await common_stub.GetServiceStatus(common.Empty())).health == "DEGRADED"

        # Stop 与未收敛 I/O 并发：它在 wait_settled 窗口内等待原生调用。
        stop_task = asyncio.ensure_future(
            dg_stub.StopGeneration(
                dg.StopGenerationRequest(
                    generation_id=first.generation_id, expected_revision=revision
                )
            )
        )
        await asyncio.sleep(0.3)  # Stop 已进入结算等待，原生调用仍挂起
        # 原生 I/O 未可信收敛：第二个 Generation 不得取得目标。
        rival = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert rival.accepted is False
        assert rival.error_code == "RESOURCE_BUSY"

        # 底层调用安全返回成功（仍在原生硬截止内）→ Stop 收敛为 STOPPED。
        gate.set()
        stopped = await asyncio.wait_for(stop_task, timeout=5.0)
        assert stopped.accepted
        assert stopped.actual_state == "STOPPED", stopped
        # 目标释放后新 Generation 立即可取得。
        retry = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert retry.accepted, retry.message
    finally:
        await channel.close()
