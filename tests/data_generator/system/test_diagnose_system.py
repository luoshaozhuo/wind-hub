"""UC-05 System：独立进程的只读诊断——分阶段证据、空目标、业务拒绝与取消安全。

覆盖 §14.2 标识：
test_simulate_replay_stages / test_empty_and_bad_mapping /
test_grpc_rejection / test_no_write_and_cancel。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys

import grpc
import pytest
import yaml

from contracts import common_pb2 as common

from ..conftest import (
    REPO_ROOT,
    SRC_PATH,
    dg_stubs,
    replay_document,
    ship_scenarios_document,
)

pytestmark = [pytest.mark.system, pytest.mark.reliability]

VARIABLE = "GW01.pitch_angle"
ALL_STAGES = ["source", "mapping", "network", "grpc", "write"]


def _stages_map(response: common.DiagnoseResponse) -> dict[str, common.DiagnosticStage]:
    return {stage.stage: stage for stage in response.stages}


@pytest.mark.end_to_end
async def test_simulate_replay_stages(
    start_real_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-05-S01：模拟/重放场景逐阶段真实检查，证据与覆盖范围准确。"""
    gateway = await start_real_gateway(
        endpoint_id="gw-a", devices={"GW01": [("pitch_angle", "float32", "feedback")]}
    )
    history_rows = [
        "GW02,a02_dm_pitchangle1,2026-01-01T00:00:00Z,1.5,GOOD,h1",
        "GW02,a02_dm_pitchangle1,2026-01-01T00:00:01Z,2.5,GOOD,h2",
    ]
    service_path = configs_root(
        scenarios=replay_document(
            {"gw-a": gateway.data_bind},
            file="history/diag.csv",
            speed=100.0,
            mappings=[("gw-a", VARIABLE)],
        ),
        history={"diag.csv": history_rows},
    )
    bind, _process = await dg_subprocess(service_path)
    channel, _dg_stub, common_stub = await dg_stubs(bind)
    try:
        replay = await common_stub.Diagnose(
            common.DiagnoseRequest(
                target_id="demo-replay",
                stages=["source", "mapping", "network", "grpc"],
            )
        )
        assert replay.overall_status == "PASS", [
            (s.stage, s.status, s.message) for s in replay.stages
        ]
        stages = _stages_map(replay)
        assert all(
            stages[name].status == "PASS" for name in ("source", "mapping", "network", "grpc")
        )
        source_evidence = {item.name: item.value for item in stages["source"].evidence}
        assert int(source_evidence["history_bytes"]) > 0

        # 含 write 阶段：恒 SKIPPED 且不伪造 PASS，总体降级 UNKNOWN。
        full = await common_stub.Diagnose(
            common.DiagnoseRequest(target_id="demo-replay", stages=ALL_STAGES)
        )
        assert _stages_map(full)["write"].status == "SKIPPED"
        assert full.overall_status == "UNKNOWN"
        assert any("write skipped" in note for note in full.limitations)
    finally:
        await channel.close()


@pytest.mark.no_targets
async def test_empty_and_bad_mapping(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-05-S02：无目标显式 UNKNOWN；映射缺变量 FAIL，不虚构 PASS。"""
    catalog = {"gw-a": [("GW01.other", "float32", True)]}
    fault = await start_fault_gateway(catalog)
    service_path = configs_root(
        scenarios=ship_scenarios_document({"gw-a": fault.bind}, mappings=[("gw-a", VARIABLE)])
    )
    bind, _process = await dg_subprocess(service_path)
    channel, _dg_stub, common_stub = await dg_stubs(bind)
    try:
        missing = await common_stub.Diagnose(common.DiagnoseRequest(target_id="ghost"))
        assert missing.overall_status == "UNKNOWN"
        assert "NO_DIAGNOSTIC_TARGETS" in missing.limitations

        bad = await common_stub.Diagnose(
            common.DiagnoseRequest(target_id="demo-random", stages=ALL_STAGES)
        )
        assert bad.overall_status == "FAIL"
        stages = _stages_map(bad)
        assert stages["grpc"].status == "FAIL"
        assert "missing Gateway variable" in stages["grpc"].message
        # 故障仅在 grpc 阶段定位：source/mapping 自身仍为 PASS。
        assert stages["source"].status == "PASS"
        assert stages["mapping"].status == "PASS"
    finally:
        await channel.close()


@pytest.mark.fault_injection
async def test_grpc_rejection(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """UC-05-S03：端口可达、目录可读但业务不可写——精确定位业务层而非报通路通。"""
    catalog = {"gw-a": [(VARIABLE, "float32", False)]}  # 变量存在但不可写
    fault = await start_fault_gateway(catalog)
    service_path = configs_root(
        scenarios=ship_scenarios_document({"gw-a": fault.bind}, mappings=[("gw-a", VARIABLE)])
    )
    bind, _process = await dg_subprocess(service_path)
    channel, _dg_stub, common_stub = await dg_stubs(bind)
    try:
        response = await common_stub.Diagnose(
            common.DiagnoseRequest(target_id="demo-random", stages=ALL_STAGES)
        )
        stages = _stages_map(response)
        assert stages["network"].status == "PASS"
        assert stages["grpc"].status == "FAIL"
        # 实现把不可写归入标量不匹配文案；定位仍精确在业务 grpc 层。
        assert "type mismatch" in stages["grpc"].message or "writable" in stages["grpc"].message
        assert response.overall_status == "FAIL"
        # 诊断全程不调用正式写入。
        assert fault.service.update_calls["gw-a"] == []
    finally:
        await channel.close()


@pytest.mark.safety
@pytest.mark.deadline
async def test_no_write_and_cancel(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """P2-2：默认 permit_wait=0.5s 有界等待，超时 RESOURCE_EXHAUSTED；取消不泄露许可。

    同时保留首轮铁律：客户端截止为 DEADLINE_EXCEEDED；诊断全程零写入、write 恒 SKIPPED。
    """
    gate = asyncio.Event()
    catalog = {"gw-a": [(VARIABLE, "float32", True)]}
    fault = await start_fault_gateway(catalog, {"gw-a": {"list_gate": gate, "list_delay": 0.6}})
    service_path = configs_root(
        scenarios=ship_scenarios_document({"gw-a": fault.bind}, mappings=[("gw-a", VARIABLE)]),
        diagnose_concurrent=1,  # 未配置 permit_wait_seconds => 生产默认 0.5s
    )
    bind, _process = await dg_subprocess(service_path)
    channel, _dg_stub, common_stub = await dg_stubs(bind)
    try:
        # 1) 客户端 0.3s 截止：服务端目录检查仍在进行，调用以 DEADLINE_EXCEEDED 结束。
        with pytest.raises(grpc.RpcError) as deadline:
            await common_stub.Diagnose(
                common.DiagnoseRequest(
                    target_id="demo-random",
                    stages=["source", "mapping", "network", "grpc", "write"],
                ),
                timeout=0.3,
            )
        assert deadline.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED
        gate.set()
        await asyncio.sleep(1.0)

        # 2) P2-2：在途诊断占用唯一许可；第二个诊断排队 ~0.5s 后被有界拒绝，
        #    既不立即拒绝（等待真实发生）也不无界排队。
        gate2 = asyncio.Event()
        fault.service._behaviors = {"gw-a": {"list_gate": gate2}}  # noqa: SLF001
        holder = asyncio.ensure_future(
            common_stub.Diagnose(common.DiagnoseRequest(target_id="demo-random", stages=["grpc"]))
        )
        await asyncio.sleep(0.2)  # 确保 holder 已取得许可
        began = asyncio.get_running_loop().time()
        with pytest.raises(grpc.RpcError) as exhausted:
            await common_stub.Diagnose(
                common.DiagnoseRequest(target_id="demo-random", stages=["grpc"])
            )
        waited = asyncio.get_running_loop().time() - began
        assert exhausted.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert 0.4 <= waited <= 1.2, waited
        gate2.set()
        await holder

        # 3) 释放后容量恢复、诊断可成功；全程零写入。
        recovered = await common_stub.Diagnose(
            common.DiagnoseRequest(target_id="demo-random", stages=ALL_STAGES)
        )
        write_stage = _stages_map(recovered)["write"]
        assert write_stage.status == "SKIPPED"
        assert fault.service.update_calls["gw-a"] == []
    finally:
        await channel.close()


@pytest.mark.safety
async def test_permit_wait_allows_bounded_queueing(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """P2-2：许可在等待窗口内释放时，排队诊断成功，不被 RESOURCE_EXHAUSTED 误杀。"""
    catalog = {"gw-a": [(VARIABLE, "float32", True)]}
    # 首个诊断 grpc 阶段耗时 ~0.35s（list_delay），第二个在 2s 窗口内排队后成功。
    fault = await start_fault_gateway(catalog, {"gw-a": {"list_delay": 0.35}})
    service_path = configs_root(
        scenarios=ship_scenarios_document({"gw-a": fault.bind}, mappings=[("gw-a", VARIABLE)]),
        diagnose_concurrent=1,
        diagnose_wait=2.0,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, _dg_stub, common_stub = await dg_stubs(bind)
    try:
        first, second = await asyncio.gather(
            common_stub.Diagnose(common.DiagnoseRequest(target_id="demo-random", stages=["grpc"])),
            common_stub.Diagnose(common.DiagnoseRequest(target_id="demo-random", stages=["grpc"])),
        )
        assert first.overall_status == "PASS"
        assert second.overall_status == "PASS"
    finally:
        await channel.close()


@pytest.mark.safety
async def test_cancelled_waiter_does_not_leak_permit(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    """P2-2：排队等待方被客户端取消后，许可不泄漏，后续诊断立即可用。"""
    gate = asyncio.Event()
    catalog = {"gw-a": [(VARIABLE, "float32", True)]}
    fault = await start_fault_gateway(catalog, {"gw-a": {"list_gate": gate}})
    service_path = configs_root(
        scenarios=ship_scenarios_document({"gw-a": fault.bind}, mappings=[("gw-a", VARIABLE)]),
        diagnose_concurrent=1,
        diagnose_wait=5.0,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, _dg_stub, common_stub = await dg_stubs(bind)
    try:
        holder = asyncio.ensure_future(
            common_stub.Diagnose(common.DiagnoseRequest(target_id="demo-random", stages=["grpc"]))
        )
        await asyncio.sleep(0.25)  # holder 已占用唯一许可并挂在目录检查上
        waiter = asyncio.ensure_future(
            common_stub.Diagnose(common.DiagnoseRequest(target_id="demo-random", stages=["grpc"]))
        )
        await asyncio.sleep(0.25)  # waiter 已进入许可等待
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        gate.set()
        await holder
        # 若取消的等待方吞掉过许可，这里会永久等待直到 RESOURCE_EXHAUSTED。
        next_response = await common_stub.Diagnose(
            common.DiagnoseRequest(target_id="demo-random", stages=ALL_STAGES)
        )
        assert _stages_map(next_response)["write"].status == "SKIPPED"
        assert fault.service.update_calls["gw-a"] == []
    finally:
        await channel.close()


@pytest.mark.safety
async def test_invalid_permit_wait_rejected_at_startup(configs_root) -> None:
    """P2-2：permit_wait_seconds 越界（合法 0.01..10.0）进程 --check 非零退出。"""
    service_path = configs_root(
        scenarios=ship_scenarios_document({"gw-a": "127.0.0.1:9"}, mappings=[("gw-a", VARIABLE)])
    )
    for bad_value in (0.0, 0.005, 10.5):
        document = yaml.safe_load(service_path.read_text(encoding="utf-8"))
        document["diagnose"]["permit_wait_seconds"] = bad_value
        service_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC_PATH) + os.pathsep + env.get("PYTHONPATH", "")
        check = subprocess.run(
            [
                sys.executable,
                "-m",
                "data_generator.main",
                "--config",
                str(service_path),
                "--check",
            ],
            cwd=str(REPO_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=30,
            check=False,
        )
        assert check.returncode != 0, bad_value
        assert b"permit_wait_seconds" in check.stdout, check.stdout
