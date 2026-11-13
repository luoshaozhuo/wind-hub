"""请求级物理步执行证据：PENDING/APPLIED/UNKNOWN 语义与主体隔离。

使用出厂 ``wind-plant-physical`` 物理场景与真实 PhysicalEngine，通过阻塞
``advance()`` 精确控制 completed_step 推进，验证：
- effective_step 是预留步，步完成前只能观测 PENDING；
- advance 成功完成后 GetInputExecution 证据为 APPLIED；
- 认证主体/request_id/Generation 不匹配只能得到 UNKNOWN；
- Generation 停止后不得伪报 APPLIED；
- 同 (caller, request_id) 幂等重放返回同一步，异指纹冲突拒绝。
"""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import Any

import pytest
import yaml

from data_generator.application.runtime import Runtime
from data_generator.domain.model import DataBatch, DeliveryResult, GeneratorError, OutputTarget
from data_generator.infrastructure.config import load_settings
from data_generator.infrastructure.snapshots import recover_transaction

REPO_ROOT = Path(__file__).resolve().parents[3]
PHYSICAL_SERVICE = REPO_ROOT / "configs" / "services" / "data-generator-sim.example.yaml"
SCENARIO_ID = "wind-plant-physical"
DEVICE_ID = "WTG_SIM01"
POINT_ID = "power_limit_setpoint"
CALLER = "gateway-wtg-sim01"

pytestmark = [pytest.mark.component, pytest.mark.reliability]


class _FakeWriter:
    """组件层 OutputPort 替身：不建立任何真实 Gateway 连接。"""

    def __init__(self) -> None:
        self.unsafe_gateways: frozenset[str] = frozenset()
        self.unsettled_gateways: frozenset[str] = frozenset()
        self.rpc_durations: deque[tuple[float, str, float]] = deque(maxlen=256)
        self.owners: set[str] = set()
        self.sent = 0

    def has_claims(self, owner_id: str) -> bool:
        return owner_id in self.owners

    async def claim(self, scenario: Any, owner_id: str) -> None:
        self.owners.add(owner_id)

    async def release(self, scenario: Any, owner_id: str) -> bool:
        self.owners.discard(owner_id)
        return True

    async def validate(self, scenario: Any) -> None:
        return None

    async def send(self, scenario: Any, batch: DataBatch):
        self.sent += 1
        return [
            DeliveryResult(OutputTarget("gw-wtg-sim01", f"{DEVICE_ID}.{POINT_ID}"), "SUCCESS")
            for _ in batch.points
        ]

    async def wait_settled(self, gateway_ids: Any, timeout: float) -> bool:
        return True

    async def drain(self) -> None:
        return None

    async def close(self) -> bool:
        return True


def _physical_service_path(configs_root: Any) -> Path:
    """在真实 Core 配置树上装载出厂物理场景，并切换到仿真设备实例文件。

    场景间隔放大到 1s：循环在步边界之间睡眠，预留步在该窗口内确定性保持
    PENDING，避免对同步 PhysicalEngine.advance 做任何侵入式打桩。
    """
    with PHYSICAL_SERVICE.open(encoding="utf-8") as handle:
        physical_doc = yaml.safe_load(handle)
    scenario = physical_doc["scenarios"][0]
    assert scenario["scenario_id"] == SCENARIO_ID
    scenario["source"]["interval_seconds"] = 1.0
    service_path = configs_root(
        scenarios={"scenarios": physical_doc["scenarios"]},
        bind="127.0.0.1:0",
    )
    document = yaml.safe_load(service_path.read_text(encoding="utf-8"))
    document["config_sources"]["instances"]["devices"] = "instances/devices_sim.yaml"
    service_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return service_path


def _settings(service_path: Path):  # noqa: ANN202
    recover_transaction(service_path.absolute().parent.parent)
    return load_settings(service_path)


async def _start_runtime(configs_root: Any) -> tuple[Runtime, str]:
    service_path = _physical_service_path(configs_root)
    runtime = Runtime(_settings(service_path), _FakeWriter())
    state = await runtime.start(SCENARIO_ID, None, runtime.settings.revision)
    # start() 返回时循环任务可能仍在 STARTING：等待物理引擎装配并进入 RUNNING。
    deadline = asyncio.get_running_loop().time() + 5.0
    while asyncio.get_running_loop().time() < deadline:
        if state.physical_engine is not None and state.actual_state == "RUNNING":
            break
        if state.actual_state == "FAILED":
            raise RuntimeError(f"physical generation failed: {state.last_error}")
        await asyncio.sleep(0.005)
    assert state.physical_engine is not None and state.actual_state == "RUNNING"
    return runtime, state.generation_id


async def _wait_evidence(
    runtime: Runtime, generation_id: str, request_id: str, expected: str
) -> tuple[str, int]:
    deadline = asyncio.get_running_loop().time() + 5.0
    state = ""
    step = 0
    while asyncio.get_running_loop().time() < deadline:
        state, step = await runtime.get_input_execution(generation_id, CALLER, request_id)
        if state == expected:
            return state, step
        await asyncio.sleep(0.01)
    return state, step


async def test_reserved_step_pending_until_advance_completes(configs_root: Any) -> None:
    runtime, generation_id = await _start_runtime(configs_root)
    try:
        state = runtime.generations[generation_id]
        # 大间隔窗口内没有物理步完成：预留与完成语义严格分离。
        assert state.completed_step == 0
        effective_step = await runtime.update_inputs(
            generation_id,
            DEVICE_ID,
            CALLER,
            "request-pending-001",
            b"fingerprint-001",
            {POINT_ID: 1500.0},
        )
        # 预留语义：返回的是 current_step+1，completed_step 尚未抵达。
        assert effective_step == state.current_step + 1
        assert state.completed_step < effective_step

        evidence_state, evidence_step = await runtime.get_input_execution(
            generation_id, CALLER, "request-pending-001"
        )
        assert evidence_state == "PENDING"
        assert evidence_step == effective_step

        evidence_state, evidence_step = await _wait_evidence(
            runtime, generation_id, "request-pending-001", "APPLIED"
        )
        assert evidence_state == "APPLIED"
        assert evidence_step == effective_step
        assert state.completed_step >= effective_step
    finally:
        await runtime.stop(generation_id, runtime.settings.revision)


async def test_execution_evidence_requires_matching_caller_and_request(configs_root: Any) -> None:
    runtime, generation_id = await _start_runtime(configs_root)
    try:
        effective_step = await runtime.update_inputs(
            generation_id,
            DEVICE_ID,
            CALLER,
            "request-isolation-001",
            b"fingerprint-002",
            {POINT_ID: 1600.0},
        )
        # 不同认证主体不得取得原请求的执行证明。
        state, step = await runtime.get_input_execution(
            generation_id, "other-commander-cn", "request-isolation-001"
        )
        assert (state, step) == ("UNKNOWN", 0)
        # 同主体不同 request_id 同样无证据。
        state, step = await runtime.get_input_execution(
            generation_id, CALLER, "request-never-submitted"
        )
        assert (state, step) == ("UNKNOWN", 0)
        # 不存在的 Generation 不得伪报。
        state, step = await runtime.get_input_execution(
            "generation-does-not-exist", CALLER, "request-isolation-001"
        )
        assert (state, step) == ("UNKNOWN", 0)
        # 匹配三元组最终可观测 APPLIED。
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            state, step = await runtime.get_input_execution(
                generation_id, CALLER, "request-isolation-001"
            )
            if state == "APPLIED":
                break
            await asyncio.sleep(0.005)
        assert (state, step) == ("APPLIED", effective_step)
    finally:
        await runtime.stop(generation_id, runtime.settings.revision)


async def test_stopped_generation_never_reports_applied(configs_root: Any) -> None:
    runtime, generation_id = await _start_runtime(configs_root)
    state = runtime.generations[generation_id]
    effective_step = await runtime.update_inputs(
        generation_id,
        DEVICE_ID,
        CALLER,
        "request-stop-001",
        b"fingerprint-003",
        {POINT_ID: 1700.0},
    )
    pending_state, _ = await runtime.get_input_execution(generation_id, CALLER, "request-stop-001")
    assert pending_state == "PENDING"
    await runtime.stop(generation_id, runtime.settings.revision)

    evidence_state, evidence_step = await runtime.get_input_execution(
        generation_id, CALLER, "request-stop-001"
    )
    assert evidence_state == "UNKNOWN"
    assert evidence_step == effective_step
    assert state.completed_step < effective_step


async def test_idempotent_replay_returns_same_reserved_step(configs_root: Any) -> None:
    runtime, generation_id = await _start_runtime(configs_root)
    try:
        first_step = await runtime.update_inputs(
            generation_id,
            DEVICE_ID,
            CALLER,
            "request-replay-001",
            b"fingerprint-004",
            {POINT_ID: 1800.0},
        )
        # 同身份同指纹：返回同一步，不产生新预留。
        replay_step = await runtime.update_inputs(
            generation_id,
            DEVICE_ID,
            CALLER,
            "request-replay-001",
            b"fingerprint-004",
            {POINT_ID: 1800.0},
        )
        assert replay_step == first_step
        # 同身份异指纹：确定性冲突，拒绝新执行。
        with pytest.raises(GeneratorError, match="request_id already used with different input"):
            await runtime.update_inputs(
                generation_id,
                DEVICE_ID,
                CALLER,
                "request-replay-001",
                b"fingerprint-changed",
                {POINT_ID: 1900.0},
            )
    finally:
        await runtime.stop(generation_id, runtime.settings.revision)
