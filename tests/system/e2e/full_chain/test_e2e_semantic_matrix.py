"""E2E-08/I-09：完整分母、复用证据与真实语义 delta。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import grpc
import pytest
from google.protobuf.json_format import MessageToDict
from google.protobuf.timestamp_pb2 import Timestamp

from cli.main import _command_words
from cli.runtime import Runtime
from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc
from tests.cli.test_r1f3_strict_cli_e2e import ADMIN_TOKEN

from .conftest import FullChainHarness
from .semantic_matrix import (
    CLI_ROWS,
    COMMON_DEPLOYMENT_ROWS,
    COMMON_DEPLOYMENTS,
    COMMON_METHODS,
    RPC_ROWS,
    SemanticEntry,
)

pytestmark = [
    pytest.mark.end_to_end,
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]

_RPC_MODULES = {
    "CommonService": "common",
    "CollectorService": "collector",
    "CommanderService": "commander",
    "SinkService": "sink_service",
    "IndustrialGatewayService": "industrial_gateway",
    "DataGeneratorService": "data_generator",
}


def test_semantic_matrix_has_exact_73_cli_51_rpc_and_54_common_deployments() -> None:
    """Descriptor/CLI 注册树是分母；Common 部署视图不混入 51 唯一 RPC。"""
    cli = {row.key: row for row in CLI_ROWS}
    rpc = {row.key: row for row in RPC_ROWS}
    assert len(CLI_ROWS) == len(cli) == 73
    assert len(RPC_ROWS) == len(rpc) == 51
    assert set(cli) == set(_command_words())
    assert len(COMMON_DEPLOYMENT_ROWS) == 54
    assert len(COMMON_DEPLOYMENTS) == 6
    assert len(COMMON_METHODS) == 9
    assert {(row.deployment, row.key) for row in COMMON_DEPLOYMENT_ROWS} == {
        (deployment, f"CommonService.{method}")
        for deployment in COMMON_DEPLOYMENTS
        for method in COMMON_METHODS
    }

    descriptors: set[str] = set()
    for service, module_name in _RPC_MODULES.items():
        module = importlib.import_module(f"contracts.{module_name}_pb2")
        descriptor = module.DESCRIPTOR.services_by_name[service]
        for method in descriptor.methods:
            assert not method.client_streaming and not method.server_streaming
            descriptors.add(f"{service}.{method.name}")
            assert method.input_type.full_name and method.output_type.full_name
    assert descriptors == set(rpc)

    for row in (*CLI_ROWS, *RPC_ROWS, *COMMON_DEPLOYMENT_ROWS):
        assert row.action in {"reuse", "delta"}, row
        assert row.normal and row.rejection and row.auth, row
        assert row.object_id and row.readback and row.side_effect, row
        assert row.idempotency and row.evidence and row.request, row
        assert (row.expected_exit is None) != (row.expected_status is None), row
        if row.expected_exit is not None:
            assert row.expected_exit in {0, 1, 2, 3, 4}, row
        else:
            assert row.expected_status in {
                "OK",
                "INVALID_ARGUMENT",
                "NOT_FOUND",
                "FAILED_PRECONDITION",
                "PERMISSION_DENIED",
            }, row
        if row.action == "reuse":
            assert row.evidence != "tests/system/e2e/full_chain/test_e2e_surface_matrix.py", row


def _last_cli_call(harness: FullChainHarness) -> dict[str, Any]:
    lines = harness.cli.log.read_text(encoding="utf-8").splitlines()
    assert lines
    observed = json.loads(lines[-1])
    assert isinstance(observed, dict)
    return observed


def _envelope_status(expected_exit: int) -> str:
    return {0: "SUCCESS", 1: "FAILURE", 2: "FAILURE", 3: "UNKNOWN", 4: "PARTIAL"}[expected_exit]


def _assert_cli_payload(row: SemanticEntry, envelope: dict[str, Any]) -> None:
    """成功行必须返回与所选真实对象一致的数据，而不只是退出码 0。"""
    if row.expected_exit != 0 or row.key in {"help", "exit", "data-generator variables update"}:
        return
    data = envelope.get("data")
    assert data is not None, (row.key, envelope)
    if row.key == "config diff":
        assert data["complete"] is True
    elif row.key == "service list":
        assert {"gateway", "collector", "commander", "sink", "data-generator"} <= {
            item["service"] for item in data
        }
    elif row.key == "service status":
        assert len(data) >= 6 and all(item["process"] for item in data)
    elif row.key == "service info":
        assert data["service_id"] == "industrial_gateway"
    elif row.key == "service capabilities":
        assert len(data["rpc_names"]) == 19
    elif row.key == "net tcp":
        assert data["tcp_state"] == "OPEN"
    elif row.key == "gateway list":
        assert len(data["gateways"]) == 6
    elif row.key == "gateway status":
        assert data["gateway"]["gateway_id"] == "gw-wtg_sim01"
    elif row.key == "gateway variables":
        assert any(item["point_id"] == "wind_speed" for item in data["variables"])
    elif row.key == "gateway read":
        assert data["values"][0]["point_id"] == "wind_speed"
    elif row.key == "collector tasks":
        assert any(item["task_id"] == "WTG_SIM01" for item in data["tasks"])
    elif row.key == "collector devices":
        assert any(item["device_id"] == "WTG_SIM01" for item in data["devices"])
    elif row.key == "collector device":
        assert data["device"]["device_id"] == "WTG_SIM01"
    elif row.key == "collector points":
        assert any(item["point_id"] == "wind_speed" for item in data["points"])
    elif row.key == "collector point":
        assert data["point"]["point_id"] == "wind_speed"
    elif row.key == "collector task":
        assert data["task"]["task_id"] == "WTG_SIM01"
    elif row.key == "collector latest":
        assert any(item["point_id"] == "wind_speed" for item in data["samples"])
    elif row.key == "collector watch":
        assert data["device_id"] == "WTG_SIM01" and data["iterations"] == 1
    elif row.key == "collector publishing":
        assert len(data["channels"]) == 1 and data["channels"][0]["nats_connected"] is True
    elif row.key == "sink deliveries":
        assert any(item["delivery_id"] == "ems_power" for item in data["deliveries"])
    elif row.key == "sink delivery":
        assert data["delivery"]["delivery_id"] == "ems_power"
    elif row.key == "data-generator devices":
        assert any(item["device_id"] == "WTG_SIM01" for item in data["devices"])
    elif row.key == "data-generator device":
        assert data["device"]["device_id"] == "WTG_SIM01"
    elif row.key == "data-generator scenarios":
        assert any(item["scenario_id"] == "wind-plant-physical" for item in data["scenarios"])
    elif row.key == "data-generator status":
        assert data["generation"]["actual_state"] == "RUNNING"
    elif row.key == "data-generator variables list":
        assert data["variables"]
    elif row.key == "commander devices":
        assert any(item["device_id"] == "PPC_SIM01" for item in data["devices"])
    elif row.key == "commander device":
        assert data["device"]["device_id"] == "PPC_SIM01"
    elif row.key == "commander points":
        assert any(item["point_id"] == "agc_enable" for item in data["points"])
    elif row.key.startswith("diagnose "):
        assert isinstance(data, dict) and data
    elif row.key.endswith(" metrics") or row.key.endswith(" events"):
        assert isinstance(data, dict)


async def _cli_exact(
    harness: FullChainHarness,
    row: SemanticEntry,
    args: tuple[str, ...],
    *,
    token: str | None = ADMIN_TOKEN,
) -> dict[str, Any]:
    envelope = await harness.cli_call(
        harness.cli_rt,
        *args,
        check=False,
        token=token,
        timeout=60,
    )
    observed = _last_cli_call(harness)
    assert row.expected_exit is not None
    assert observed["returncode"] == row.expected_exit, {
        "command": row.key,
        "expected": row.expected_exit,
        "observed": observed,
        "envelope": envelope,
    }
    if envelope:
        assert envelope.get("status") == _envelope_status(row.expected_exit), {
            "command": row.key,
            "envelope": envelope,
        }
        if row.expected_error is not None:
            assert envelope.get("error_code") == row.expected_error, {
                "command": row.key,
                "expected_error": row.expected_error,
                "envelope": envelope,
            }
    elif row.expected_error is not None:
        raise AssertionError(f"{row.key}: expected structured error {row.expected_error}")
    if envelope:
        _assert_cli_payload(row, envelope)
    observed["envelope"] = envelope
    return observed


def _arguments(row: SemanticEntry, context: dict[str, str]) -> tuple[str, ...]:
    raw = row.request[len(row.key) :].strip()
    return tuple(part.format_map(context) for part in raw.split()) if raw else ()


def _tree_digest(harness: FullChainHarness) -> str:
    digest = hashlib.sha256()
    roots = [harness.plant]
    if harness.sink is not None:
        roots.append(harness.sink.root)
    for root in roots:
        for path in sorted(root.rglob("*.yaml")):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


async def _start_generation(harness: FullChainHarness) -> str:
    started = await harness.cli_call(
        harness.cli_rt,
        "data-generator",
        "start",
        "wind-plant-physical",
        "--scenario-version",
        "1.0.0",
        "--yes",
        timeout=60,
    )
    generation_id = str(started["data"]["generation_id"])
    assert started["data"]["accepted"] is True
    harness.generation_id = generation_id
    await harness.poll(
        harness.cli_rt,
        "data-generator",
        "status",
        generation_id,
        predicate=lambda data: data.get("generation", {}).get("actual_state") == "RUNNING",
        timeout=60,
    )
    return generation_id


async def _stop_generation(harness: FullChainHarness, generation_id: str | None) -> None:
    if generation_id is None:
        return
    await harness.cli_call(
        harness.cli_rt,
        "data-generator",
        "stop",
        generation_id,
        "--yes",
        check=False,
        timeout=60,
    )
    if harness.generation_id == generation_id:
        harness.generation_id = None


@pytest.mark.timeout(600)
async def test_cli_semantic_delta_has_exact_exit_and_error_contracts(
    full_chain: FullChainHarness,
) -> None:
    """只重跑缺语义的叶子；进程/控制/Sink/Gateway 副作用行复用专项证据。"""
    root = full_chain.root / "semantic-cli"
    left = root / "left"
    right = root / "right"
    empty = root / "empty"
    for directory in (left, right, empty):
        directory.mkdir(parents=True, exist_ok=True)
    attestation = root / "untrusted-attestation.bin"
    attestation.write_bytes(b"not-a-trusted-isolation-proof")
    batch = root / "batch.json"
    batch.write_text(
        json.dumps(
            [
                {
                    "device_id": "PPC_SIM01",
                    "point_id": "agc_enable",
                    "value": 1,
                    "expected_revision": "filled-by-reuse-scenario",
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                }
            ]
        ),
        encoding="utf-8",
    )
    context = {
        "left": str(left),
        "right": str(right),
        "empty": str(empty),
        "attestation": str(attestation),
        "batch": str(batch),
        "gateway_port": str(full_chain.ports["gateway"]),
        "zero_revision": "0" * 64,
        "request_id": f"semantic-cli-{uuid4()}",
        "task_id": "",
        "generation_id": "",
    }
    activity_start = datetime.now(UTC)
    generation_id = await _start_generation(full_chain)
    context["generation_id"] = generation_id
    await full_chain.cli_call(full_chain.cli_rt, "collector", "start", "WTG_SIM01", "--yes")
    assert full_chain.sink is not None
    await full_chain.sink.wait_redis_value(
        full_chain.redis_key("WTG_SIM01", "wind_speed"),
        timeout=90,
        match=lambda item: datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00"))
        >= activity_start,
    )
    before_config = _tree_digest(full_chain)
    calls: list[dict[str, Any]] = []
    try:
        for row in CLI_ROWS:
            if row.action != "delta" or row.key == "data-generator variables update":
                continue
            if row.key == "collector task" and not context["task_id"]:
                tasks = await full_chain.cli_call(full_chain.cli_rt, "collector", "tasks")
                context["task_id"] = str(tasks["data"]["tasks"][0]["task_id"])
            observed = await _cli_exact(full_chain, row, _arguments(row, context))
            calls.append(
                {
                    "command_id": row.key,
                    "expected_exit": row.expected_exit,
                    "observed_exit": observed["returncode"],
                    "expected_error": row.expected_error,
                    "observed_error": observed["envelope"].get("error_code"),
                    "object_id": row.object_id,
                    "readback": row.readback,
                    "side_effect": row.side_effect,
                    "idempotency": row.idempotency,
                    "action": row.action,
                }
            )
        assert _tree_digest(full_chain) == before_config
        full_chain.record(
            "E2E-08-CLI-SEMANTIC-DELTA",
            total=73,
            delta=len(calls),
            reuse=73 - len(calls) - 1,
            dedicated_generator_update=1,
            calls=calls,
        )
    finally:
        await full_chain.cli_call(
            full_chain.cli_rt, "collector", "stop", "WTG_SIM01", "--yes", check=False
        )
        await _stop_generation(full_chain, generation_id)


@pytest.mark.timeout(120)
async def test_collector_cli_wrong_and_missing_token_cannot_start_task(
    full_chain: FullChainHarness,
) -> None:
    tasks = await full_chain.cli_call(full_chain.cli_rt, "collector", "tasks")
    task_id = str(tasks["data"]["tasks"][0]["task_id"])
    before = await full_chain.cli_call(full_chain.cli_rt, "collector", "task", task_id)
    row = next(row for row in CLI_ROWS if row.key == "collector start")
    for token, expected_error in (
        ("wrong-semantic-token", "PERMISSION_DENIED"),
        (None, "PermissionError"),
    ):
        observed = await _cli_exact(
            full_chain,
            SemanticEntry(
                **{
                    **row.__dict__,
                    "expected_exit": 2,
                    "expected_error": expected_error,
                }
            ),
            ("collector", "start", task_id, "--yes"),
            token=token,
        )
        assert ADMIN_TOKEN not in observed["stdout"] + observed["stderr"]
    after = await full_chain.cli_call(full_chain.cli_rt, "collector", "task", task_id)
    assert after["data"] == before["data"]
    full_chain.record("E2E-08-COLLECTOR-AUTH", task_id=task_id, unchanged=True)


def _snapshot_content(snapshot: common.GetConfigSnapshotResponse) -> tuple[Any, ...]:
    return (
        snapshot.revision,
        tuple(
            (item.relative_path, bytes(item.content), item.size_bytes, item.sha256)
            for item in snapshot.files
        ),
    )


def _rpc_metadata(runtime: Runtime, deployment: str, method: str) -> Any:
    service = "collector" if deployment.startswith("collector_") else deployment
    return runtime.get_rpc_metadata(service, method)


@pytest.mark.timeout(300)
async def test_common_service_semantics_on_all_six_deployments(
    full_chain: FullChainHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """执行 6×9 部署矩阵，配置拒绝逐服务比较 DISK 与文件不变。"""
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", ADMIN_TOKEN)
    runtime = Runtime(full_chain.cli_rt, "json")
    expected_identity = {
        "gateway": ("industrial_gateway", "gateway"),
        "collector_realtime": (full_chain.rt_collector, "collector"),
        "collector_reliable": (full_chain.reliable_collector, "collector"),
        "commander": ("commander", "control"),
        "sink": ("sink-west", "main"),
        "data-generator": ("data_generator", "generator"),
    }
    expected_rpc_count = {
        "gateway": 19,
        "collector_realtime": 20,
        "collector_reliable": 20,
        "commander": 17,
        "sink": 13,
        "data-generator": 18,
    }
    targets = {
        "gateway": "gw-wtg_sim01",
        "collector_realtime": "WTG_SIM01",
        "collector_reliable": "WTG_SIM01",
        "commander": "PPC_SIM01",
        "sink": "ems_power",
        "data-generator": "",
    }
    records: list[dict[str, Any]] = []
    try:
        for deployment in COMMON_DEPLOYMENTS:
            stub = common_rpc.CommonServiceStub(runtime.channel(deployment))
            before: common.GetConfigSnapshotResponse | None = None
            before_tree = _tree_digest(full_chain)
            rows = [row for row in COMMON_DEPLOYMENT_ROWS if row.deployment == deployment]
            for row in rows:
                method = row.key.rsplit(".", 1)[1]
                malformed = common.SnapshotFile(
                    relative_path="services/semantic-invalid.yaml",
                    content=b"schema_version: 1\n",
                    size_bytes=999,
                    sha256="0" * 64,
                )
                revision = before.revision if before is not None else "0" * 64
                requests = {
                    "GetServiceInfo": common.Empty(),
                    "GetServiceStatus": common.Empty(),
                    "GetCapabilities": common.Empty(),
                    "GetConfigSnapshot": common.GetConfigSnapshotRequest(
                        scope=common.SNAPSHOT_SCOPE_DISK
                    ),
                    "PushConfigSnapshot": common.PushConfigSnapshotRequest(
                        files=[malformed], expected_revision=revision
                    ),
                    "ApplyConfigSnapshot": common.ApplyConfigSnapshotRequest(
                        candidate_id=f"missing-semantic-{deployment}",
                        expected_revision=revision,
                    ),
                    "GetMetrics": common.GetMetricsRequest(
                        window_ms=60000 if deployment == "sink" else 1000
                    ),
                    "GetEvents": common.GetEventsRequest(page_size=1),
                    "Diagnose": common.DiagnoseRequest(
                        target_id=targets[deployment],
                        timeout_ms=1000,
                        correlation_id=f"semantic-{deployment}",
                    ),
                }
                record: dict[str, Any] = {
                    "deployment": deployment,
                    "method": method,
                    "expected_status": row.expected_status,
                    "expected_detail": row.expected_detail,
                }
                try:
                    response = await getattr(stub, method)(
                        requests[method],
                        timeout=runtime.target(deployment).timeout,
                        metadata=_rpc_metadata(runtime, deployment, method),
                    )
                    observed = "OK"
                    record["response"] = MessageToDict(
                        response,
                        preserving_proto_field_name=True,
                        always_print_fields_with_no_presence=True,
                    )
                except grpc.aio.AioRpcError as exc:
                    observed = exc.code().name
                    response = None
                    record["details"] = exc.details()
                record["observed_status"] = observed
                assert observed == row.expected_status, record

                if method == "GetServiceInfo" and response is not None:
                    assert (response.service_id, response.process_role) == expected_identity[
                        deployment
                    ]
                elif method == "GetCapabilities" and response is not None:
                    assert len(response.rpc_names) == expected_rpc_count[deployment]
                    names = set(response.rpc_names)
                    for common_method in COMMON_METHODS:
                        assert names & {common_method, f"CommonService.{common_method}"}
                elif method == "GetConfigSnapshot" and response is not None:
                    assert response.files and len(response.revision) == 64
                    before = response
                elif method == "PushConfigSnapshot" and response is not None:
                    assert response.valid is False
                    assert not response.candidate_id and response.validation
                elif method == "ApplyConfigSnapshot" and response is not None:
                    assert deployment == "data-generator"
                    assert response.file_commit == "FAILED"
                    assert response.effective_revision == revision
                    assert response.apply_results[0].status == "FAILED"
                records.append(record)

            assert _tree_digest(full_chain) == before_tree, deployment
            if deployment != "gateway":
                assert before is not None, deployment
                after = await stub.GetConfigSnapshot(
                    common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_DISK),
                    timeout=runtime.target(deployment).timeout,
                    metadata=_rpc_metadata(runtime, deployment, "GetConfigSnapshot"),
                )
                assert _snapshot_content(after) == _snapshot_content(before), deployment
        assert len(records) == 54
        full_chain.record("I-09-COMMON-SEMANTICS", unique_rpcs=9, deployments=6, calls=records)
    finally:
        await runtime.close()


async def _wait_input_applied(
    stub: dg_rpc.DataGeneratorServiceStub,
    generation_id: str,
    request_id: str,
) -> dg.GetInputExecutionResponse:
    for _ in range(100):
        execution = await stub.GetInputExecution(
            dg.GetInputExecutionRequest(generation_id=generation_id, request_id=request_id),
            timeout=10,
        )
        if execution.state == "APPLIED":
            return execution
        assert execution.state == "PENDING", execution
        await asyncio.sleep(0.1)
    raise AssertionError(f"Generator input did not reach APPLIED: {request_id}")


async def _wait_recording_value(
    harness: FullChainHarness,
    generation_id: str,
    point_id: str,
    expected: float,
    device_id: str = "WTG_SIM01",
) -> dict[str, Any]:
    for _ in range(100):
        rows = harness.read_recording(generation_id)
        for row in rows:
            for point in row["points"]:
                if (
                    point["device_id"] == device_id
                    and point["point_id"] == point_id
                    and abs(float(point["value"]) - expected) < 1e-6
                ):
                    return row
        await asyncio.sleep(0.1)
    raise AssertionError(f"recording did not contain {point_id}={expected}")


@pytest.mark.timeout(180)
async def test_generator_update_is_applied_and_idempotent(full_chain: FullChainHarness) -> None:
    generation_id = await _start_generation(full_chain)
    request_id = f"semantic-dg-{uuid4()}"
    runtime = Runtime(full_chain.cli_rt, "json")
    row = next(row for row in CLI_ROWS if row.key == "data-generator variables update")
    context = {
        "generation_id": generation_id,
        "request_id": request_id,
    }
    args = _arguments(row, context)
    try:
        first = await _cli_exact(full_chain, row, args)
        accepted = first["envelope"]["data"]
        assert accepted["accepted"] is True
        assert accepted["request_id"] == request_id
        step = int(accepted["effective_step"])
        assert step > 0
        stub = dg_rpc.DataGeneratorServiceStub(runtime.channel("data-generator"))
        execution = await _wait_input_applied(stub, generation_id, request_id)
        assert execution.request_id == request_id
        assert execution.effective_step == step
        recording = await _wait_recording_value(full_chain, generation_id, "wind_speed", 12.0)

        replay = await _cli_exact(full_chain, row, args)
        assert replay["envelope"]["data"]["effective_step"] == accepted["effective_step"]
        conflict_args = tuple("13" if item == "12" else item for item in args)
        conflict_row = SemanticEntry(
            **{**row.__dict__, "expected_exit": 2, "expected_error": "ALREADY_EXISTS"}
        )
        await _cli_exact(full_chain, conflict_row, conflict_args)
        assert (await _wait_input_applied(stub, generation_id, request_id)).effective_step == step
        full_chain.record(
            "E2E-08-DG-APPLIED",
            generation_id=generation_id,
            request_id=request_id,
            effective_step=step,
            recording_step=recording.get("step"),
            replay_step=replay["envelope"]["data"]["effective_step"],
            conflict="ALREADY_EXISTS",
        )
    finally:
        await runtime.close()
        await _stop_generation(full_chain, generation_id)


def _gateway_credentials(harness: FullChainHarness, identity: str) -> grpc.ChannelCredentials:
    cert, key = harness.pki["clients"][identity]
    return grpc.ssl_channel_credentials(
        root_certificates=harness.pki["ca"].read_bytes(),
        private_key=key.read_bytes(),
        certificate_chain=cert.read_bytes(),
    )


@pytest.mark.timeout(180)
async def test_commander_certificate_simulation_applied(full_chain: FullChainHarness) -> None:
    """Commander 证书走 Gateway 仿真 RPC；CLI 证书不能越权执行。"""
    generation_id = await _start_generation(full_chain)
    request_id = f"semantic-simulation-{uuid4()}"
    gateway_status = await full_chain.cli_call(
        full_chain.cli_rt, "gateway", "status", "gw-ppc_sim01"
    )
    revision = str(gateway_status["data"]["gateway"]["revision"])
    deadline = Timestamp()
    deadline.FromDatetime(datetime.now(UTC) + timedelta(minutes=2))
    request = gw.SimulationCommandRequest(
        gateway_id="gw-ppc_sim01",
        device_id="PPC_SIM01",
        point_id="agc_enable",
        value=common.ScalarValue(uint64_value=1),
        request_id=request_id,
        expected_revision=revision,
        expires_at=deadline,
    )
    try:
        async with grpc.aio.secure_channel(
            full_chain.endpoints["gateway"],
            _gateway_credentials(full_chain, "commander"),
        ) as gateway_channel:
            stub = gw_rpc.IndustrialGatewayServiceStub(gateway_channel)
            submitted = await stub.ExecuteSimulationCommand(request, timeout=15)
            assert submitted.accepted is True
            assert submitted.request_id == request_id
            for _ in range(100):
                execution = await stub.GetSimulationExecutionResult(
                    gw.SimulationExecutionQuery(gateway_id="gw-ppc_sim01", request_id=request_id),
                    timeout=10,
                )
                if execution.state == "APPLIED":
                    break
                assert execution.state == "PENDING", execution
                await asyncio.sleep(0.1)
            else:
                raise AssertionError(f"simulation did not reach APPLIED: {execution}")
            assert execution.effective_step > 0
            replay = await stub.ExecuteSimulationCommand(request, timeout=15)
            assert replay.execution_id == submitted.execution_id
            assert replay.effective_step == execution.effective_step

            unauthorized_id = f"semantic-simulation-denied-{uuid4()}"
            async with grpc.aio.secure_channel(
                full_chain.endpoints["gateway"],
                _gateway_credentials(full_chain, "cli"),
            ) as denied_channel:
                with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                    await gw_rpc.IndustrialGatewayServiceStub(
                        denied_channel
                    ).ExecuteSimulationCommand(
                        gw.SimulationCommandRequest(
                            gateway_id=request.gateway_id,
                            device_id=request.device_id,
                            point_id=request.point_id,
                            value=request.value,
                            request_id=unauthorized_id,
                            expected_revision=request.expected_revision,
                            expires_at=deadline,
                        ),
                        timeout=10,
                    )
                assert exc_info.value.code() == grpc.StatusCode.PERMISSION_DENIED
            denied = await stub.GetSimulationExecutionResult(
                gw.SimulationExecutionQuery(gateway_id="gw-ppc_sim01", request_id=unauthorized_id),
                timeout=10,
            )
            assert denied.state == "UNKNOWN"

        async with grpc.aio.secure_channel(
            full_chain.endpoints["data-generator"],
            _gateway_credentials(full_chain, "gateway"),
        ) as generator_channel:
            applied = await _wait_input_applied(
                dg_rpc.DataGeneratorServiceStub(generator_channel),
                generation_id,
                request_id,
            )
            assert applied.effective_step == execution.effective_step
        recording = await _wait_recording_value(
            full_chain, generation_id, "agc_enable", 1.0, device_id="PPC_SIM01"
        )
        full_chain.record(
            "E2E-08-COMMANDER-SIMULATION",
            generation_id=generation_id,
            request_id=request_id,
            execution_id=execution.execution_id,
            effective_step=execution.effective_step,
            recording_step=recording.get("step"),
            unauthorized="PERMISSION_DENIED",
            unauthorized_lookup=denied.state,
        )
    finally:
        await _stop_generation(full_chain, generation_id)
