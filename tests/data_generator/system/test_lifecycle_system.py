"""UC-03/UC-08 System：断线未知结果、故障隔离与背压独立性、启动/关闭/崩溃恢复。

覆盖 §14.2 标识：
test_gateway_timeout_unknown / test_gateway_recovery_backpressure /
test_gateway_fanout_partial_failure / test_fanout_bounded_backpressure /
test_restart / test_startup_failure / test_crash_unknown_delivery /
test_unsettled_io_shutdown。

铁律：成功路径使用真实 assemble_gateway；仅超时/挂起使用冻结正式 Proto 的
FaultGatewayService。崩溃/SIGKILL/SIGTERM 全部对独立子进程真实执行。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time

import grpc
import pytest

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg

from ..conftest import (
    REPO_ROOT,
    SRC_PATH,
    FaultGatewayService,
    dg_stubs,
    metric_map,
    read_gateway_variable,
    ship_scenarios_document,
)

pytestmark = [pytest.mark.system, pytest.mark.reliability]

VARIABLE = "GW01.pitch_angle"
ONE_FLOAT = {"GW01": [("pitch_angle", "float32", "feedback")]}


async def _revision(common_stub) -> str:  # noqa: ANN001
    response = await common_stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    return str(response.revision)


async def _metrics(common_stub, window: int = 60_000) -> dict[str, float]:  # noqa: ANN001
    return metric_map(await common_stub.GetMetrics(common.GetMetricsRequest(window_ms=window)))


async def _wait_first_call(fault, gateway_id: str = "gw-a") -> None:  # noqa: ANN001
    deadline = asyncio.get_running_loop().time() + 8.0
    while asyncio.get_running_loop().time() < deadline:
        if fault.service.update_calls.get(gateway_id):
            return
        await asyncio.sleep(0.02)
    raise AssertionError("no native UpdateVariables call observed")


def _run_main(service_path, *extra: str) -> subprocess.Popen[bytes]:  # noqa: ANN001
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC_PATH) + (os.pathsep + existing if existing else "")
    return subprocess.Popen(
        [sys.executable, "-m", "data_generator.main", "--config", str(service_path), *extra],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


# --------------------------------------------------------------------------- #
# UC-03-S03 断线/未知结果、恢复重连/背压、部分成功、多目标背压独立性
# --------------------------------------------------------------------------- #


@pytest.mark.network_failure
async def test_gateway_timeout_unknown(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """超时只计 UNKNOWN，不谎报确认；隔离期不盲重试。"""
    gate = asyncio.Event()
    fault = await start_fault_gateway(
        {"gw-a": [(VARIABLE, "float32", True)]}, {"gw-a": {"gate": gate}}
    )
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.1,
            mappings=[("gw-a", VARIABLE)],
        ),
        output_timeout=0.3,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, _dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await _dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        await _wait_first_call(fault)
        # 逻辑超时后继续观察：整个隔离期只有一批底层调用，绝不盲重试。
        await asyncio.sleep(1.5)
        assert len(fault.service.update_calls["gw-a"]) == 1
        metrics = await _metrics(common_stub)
        assert metrics["data_generator.unknown_total"] >= 1
        assert metrics["data_generator.confirmed_total"] == 0
        assert metrics["gateway.gw-a.unknown_total"] >= 1
        assert (await common_stub.GetServiceStatus(common.Empty())).health == "DEGRADED"
        gate.set()
    finally:
        await channel.close()


@pytest.mark.overload
async def test_gateway_recovery_backpressure(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """挂起期间全局在途有界、源节拍被跳过；恢复后继续确认且指标不串账。"""
    FaultGatewayService.reset_concurrency()
    gate = asyncio.Event()
    fault = await start_fault_gateway(
        {"gw-a": [(VARIABLE, "float32", True)]}, {"gw-a": {"gate": gate}}
    )
    # timeout=1.0：逻辑超时 1.0s，原生硬截止 2.0s；阻塞观察窗口须落在二者之间。
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.05,
            mappings=[("gw-a", VARIABLE)],
        ),
        output_timeout=1.0,
        max_inflight=2,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)
        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        await _wait_first_call(fault)
        await asyncio.sleep(1.3)  # 许可耗尽→BACKPRESSURE_TIMEOUT，源节拍被跳过
        assert FaultGatewayService.global_max_active <= 2
        metrics = await _metrics(common_stub)
        assert metrics["data_generator.skipped_steps_total"] >= 1
        # 验收实测：batches_total 计入被背压拒绝的派发尝试；有界性体现在真实原生
        # 调用上——阻塞窗口内（逻辑超时后、原生硬截止前）只有 1 个调用在途。
        assert len(fault.service.update_calls["gw-a"]) == 1

        # 恢复（仍在原生硬截止 2.0s 内）：隔离解除后批次继续确认，账不串。
        gate.set()
        healthy_deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < healthy_deadline:
            if (await common_stub.GetServiceStatus(common.Empty())).health == "HEALTHY":
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(0.6)
        stopped = await dg_stub.StopGeneration(
            dg.StopGenerationRequest(
                generation_id=started.generation_id, expected_revision=revision
            )
        )
        assert stopped.accepted
        await asyncio.sleep(0.15)
        metrics = await _metrics(common_stub)
        assert metrics["gateway.gw-a.success_total"] >= 5
        assert metrics["gateway.gw-a.success_total"] == metrics["data_generator.confirmed_total"]
        sample = await read_gateway_variable(fault.bind, "gw-a", VARIABLE)
        assert sample.quality == "GOOD"
    finally:
        await channel.close()


@pytest.mark.network_failure
async def test_gateway_fanout_partial_failure(
    start_real_gateway,
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """同一源点扇出 A（挂起）与 B（真实）：A 隔离不回滚 B，B 持续更新。"""
    gate = asyncio.Event()
    fault = await start_fault_gateway(
        {"gw-a": [(VARIABLE, "float32", True)]}, {"gw-a": {"gate": gate}}
    )
    real = await start_real_gateway(endpoint_id="gw-b", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind, "gw-b": real.data_bind},
            interval=0.15,
            mappings=[("gw-a", VARIABLE), ("gw-b", VARIABLE)],
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
        await _wait_first_call(fault)
        await asyncio.sleep(1.6)  # A 早已逻辑超时并隔离；B 持续多轮
        assert len(fault.service.update_calls["gw-a"]) == 1
        first = await read_gateway_variable(real.data_bind, "gw-b", VARIABLE)
        assert first.quality == "GOOD"
        await asyncio.sleep(0.5)
        second = await read_gateway_variable(real.data_bind, "gw-b", VARIABLE)
        assert second.quality == "GOOD"
        assert second.value.double_value != first.value.double_value
        metrics = await _metrics(common_stub)
        assert metrics["gateway.gw-a.unknown_total"] >= 1
        assert metrics["gateway.gw-b.success_total"] >= 4
        assert metrics["gateway.gw-b.failed_total"] == 0
        gate.set()
    finally:
        await channel.close()


@pytest.mark.overload
async def test_fanout_bounded_backpressure(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """双故障目标：全局在途不超 max_inflight；单目标恢复后独立继续输出。"""
    FaultGatewayService.reset_concurrency()
    gate_a = asyncio.Event()
    gate_b = asyncio.Event()
    fault_a = await start_fault_gateway(
        {"gw-a": [(VARIABLE, "float32", True)]}, {"gw-a": {"gate": gate_a}}
    )
    fault_b = await start_fault_gateway(
        {"gw-b": [(VARIABLE, "float32", True)]}, {"gw-b": {"gate": gate_b}}
    )
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault_a.bind, "gw-b": fault_b.bind},
            interval=0.05,
            mappings=[("gw-a", VARIABLE), ("gw-b", VARIABLE)],
        ),
        # timeout=2.0：逻辑超时 2.0s，原生硬截止 4.0s，留出“阻塞观察→恢复 A”窗口。
        output_timeout=2.0,
        max_inflight=2,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)

        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        await _wait_first_call(fault_a)
        await asyncio.sleep(2.6)  # 许可被两个挂起调用占尽，源侧背压
        # 全局在途原生调用永不超过 max_inflight=2（跨两个 Gateway 聚合）。
        assert FaultGatewayService.global_max_active <= 2
        calls_b_while_blocked = len(fault_b.service.update_calls["gw-b"])
        assert calls_b_while_blocked >= 1

        # 仅恢复 A（在原生硬截止 4.0s 内）：A 独立继续；B 仍隔离，不产生新调用。
        gate_a.set()
        await asyncio.sleep(1.5)
        assert len(fault_a.service.update_calls["gw-a"]) >= calls_b_while_blocked + 2
        assert len(fault_b.service.update_calls["gw-b"]) == calls_b_while_blocked
        gate_b.set()
    finally:
        await channel.close()


# --------------------------------------------------------------------------- #
# UC-08 启动、关闭及故障后恢复
# --------------------------------------------------------------------------- #


@pytest.mark.restart
async def test_restart(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """启动前环境校验；SIGTERM 干净退出释放端口；重启是新纪元，不错误恢复旧任务。"""
    gateway = await start_real_gateway(endpoint_id="gw-a", devices=ONE_FLOAT)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": gateway.data_bind},
            interval=3600.0,
            mappings=[("gw-a", VARIABLE)],
        )
    )

    # 启动前环境/配置校验：--check 真实执行且成功。
    check = _run_main(service_path, "--check")
    assert check.wait(timeout=30) == 0

    bind, process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    revision = await _revision(common_stub)

    started = await dg_stub.StartGeneration(
        dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
    )
    assert started.accepted, started.message
    first_info = await common_stub.GetServiceInfo(common.Empty())
    await channel.close()

    began = time.monotonic()
    process.terminate()
    process.wait(timeout=20)
    assert process.returncode == 0
    assert time.monotonic() - began < 12.0

    # 同一端口/配置重启成功即证明监听端口等资源已释放。
    bind_2, _process_2 = await dg_subprocess(service_path)
    channel_2, dg_stub_2, common_2 = await dg_stubs(bind_2)
    try:
        second_info = await common_2.GetServiceInfo(common.Empty())
        assert second_info.started_at.seconds > first_info.started_at.seconds
        # 旧 Generation 不跨进程隐式恢复。
        with pytest.raises(grpc.RpcError) as missing:
            await dg_stub_2.GetGeneration(common.ObjectId(id=started.generation_id))
        assert missing.value.code() == grpc.StatusCode.NOT_FOUND
        metrics = await _metrics(common_2)
        assert metrics["data_generator.batches_total"] == 0
    finally:
        await channel_2.close()


@pytest.mark.fault_injection
async def test_startup_failure(configs_root) -> None:  # noqa: ANN001
    """配置不合法时进程非零退出，端口不监听——绝不错误报告 READY。"""
    gateway_bind = "127.0.0.1:9"  # 不会真正连接；配置校验必须先失败
    service_path = configs_root(
        scenarios=ship_scenarios_document({"gw-a": gateway_bind}, mappings=[("gw-a", VARIABLE)])
    )

    cases = {
        "syntax": b"schema_version: [unclosed\n",
        "range": service_path.read_bytes().replace(
            b"grpc_timeout_seconds: 2.0", b"grpc_timeout_seconds: 0.001"
        ),
    }
    for name, payload in cases.items():
        service_path.write_bytes(payload)
        process = _run_main(service_path)
        try:
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and process.poll() is None:
                await asyncio.sleep(0.1)
            assert process.poll() is not None, f"{name}: process stayed alive"
            assert process.returncode != 0, f"{name}: invalid config exited zero"
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            output = process.stdout.read() if process.stdout else b""
        assert output, f"{name}: failure produced no diagnostics"


@pytest.mark.recovery
async def test_crash_unknown_delivery(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """SIGKILL 崩溃后重启：在途未知批次不被隐式恢复/谎报，计数纪元从零开始。"""
    gate = asyncio.Event()
    fault = await start_fault_gateway(
        {"gw-a": [(VARIABLE, "float32", True)]}, {"gw-a": {"gate": gate}}
    )
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.1,
            mappings=[("gw-a", VARIABLE)],
        ),
        output_timeout=1.0,
    )
    bind, process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    revision = await _revision(common_stub)

    started = await dg_stub.StartGeneration(
        dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
    )
    assert started.accepted, started.message
    await _wait_first_call(fault)
    calls_before_kill = len(fault.service.update_calls["gw-a"])
    await channel.close()

    # 无清理机会的硬崩溃；随后底层调用才返回成功（结果对死者不可达）。
    process.kill()
    process.wait(timeout=10)
    gate.set()
    await asyncio.sleep(0.4)

    bind_2, _process_2 = await dg_subprocess(service_path)
    channel_2, dg_stub_2, common_2 = await dg_stubs(bind_2)
    try:
        metrics = await _metrics(common_2)
        assert metrics["data_generator.batches_total"] == 0
        assert metrics["data_generator.confirmed_total"] == 0
        with pytest.raises(grpc.RpcError) as missing:
            await dg_stub_2.GetGeneration(common.ObjectId(id=started.generation_id))
        assert missing.value.code() == grpc.StatusCode.NOT_FOUND

        # 重启后未启动任务前不会触碰 Gateway：无补偿性重放/对账。
        await asyncio.sleep(0.5)
        assert len(fault.service.update_calls["gw-a"]) == calls_before_kill + 0  # 仅崩溃前批次

        # 新任务作为全新纪元正常运行。P1-07：死者持有 fencing 租约，B 必须等其
        # TTL（lease_ms=max(10s, timeout*6)=10s，每 1/3 租期续租）自然到期后才能取得。
        revision_2 = await _revision(common_2)
        acquire_started = asyncio.get_running_loop().time()
        restarted = None
        deadline = asyncio.get_running_loop().time() + 13.0
        while asyncio.get_running_loop().time() < deadline:
            response = await dg_stub_2.StartGeneration(
                dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision_2)
            )
            if response.accepted:
                restarted = response
                break
            assert response.error_code == "RESOURCE_BUSY", response.error_code
            await asyncio.sleep(0.25)
        lease_recovery = asyncio.get_running_loop().time() - acquire_started
        assert restarted is not None, "lease never expired after SIGKILL within 13s"
        assert 0.0 < lease_recovery <= 12.0, lease_recovery
        await asyncio.sleep(0.5)
        assert len(fault.service.update_calls["gw-a"]) > calls_before_kill
    finally:
        await channel_2.close()


@pytest.mark.safety
@pytest.mark.deadline
async def test_unsettled_io_shutdown(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """在途原生调用未完成时 SIGTERM：关闭有界、退出码为零，且不把取消当底层完成。"""
    gate = asyncio.Event()
    fault = await start_fault_gateway(
        {"gw-a": [(VARIABLE, "float32", True)]}, {"gw-a": {"gate": gate}}
    )
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=0.1,
            mappings=[("gw-a", VARIABLE)],
        ),
        output_timeout=0.3,
    )
    bind, process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        revision = await _revision(common_stub)

        started = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(scenario_id="demo-random", expected_revision=revision)
        )
        assert started.accepted, started.message
        await _wait_first_call(fault)
    finally:
        await channel.close()

    # 原生调用仍挂起（gate 不开）：关闭必须有界（grace 3s + close 0.6s + 余量）。
    began = time.monotonic()
    process.terminate()
    process.wait(timeout=15)
    elapsed = time.monotonic() - began
    assert process.returncode == 0
    assert elapsed < 10.0, elapsed
    gate.set()
