"""E2E-08: execute every formal RPC against the real six-service stack.

The descriptor is the denominator.  A method is covered only after an authenticated
network call reaches the real implementation and returns either a response or a
documented, request-level rejection.  Transport failures and unimplemented handlers
remain failures rather than being counted as coverage.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
import pytest
from google.protobuf import message_factory
from google.protobuf.json_format import MessageToDict, ParseDict

from cli.main import _command_words
from cli.runtime import Runtime
from tests.cli.test_r1f3_strict_cli_e2e import ADMIN_TOKEN

from .conftest import FullChainHarness

pytestmark = [pytest.mark.asyncio, pytest.mark.end_to_end]


# ``service_key`` is the key in the real CLI configuration.  CommonService is
# deliberately exercised on Gateway; the other service-specific descriptors use
# their matching process (Collector resolves to the real realtime Collector).
RPC_SERVICES = (
    ("common", "CommonService", "gateway"),
    ("collector", "CollectorService", "collector"),
    ("commander", "CommanderService", "commander"),
    ("sink_service", "SinkService", "sink"),
    ("industrial_gateway", "IndustrialGatewayService", "gateway"),
    ("data_generator", "DataGeneratorService", "data-generator"),
)


def _requests(harness: FullChainHarness) -> dict[tuple[str, str], dict[str, Any]]:
    """Prefer valid reads; use harmless invalid identities for mutation methods."""
    disk = "SNAPSHOT_SCOPE_DISK"
    return {
        # Common: reads are valid; mutation requests are intentionally incomplete.
        ("CommonService", "GetConfigSnapshot"): {"scope": disk},
        ("CommonService", "GetMetrics"): {"window_ms": 1000},
        ("CommonService", "GetEvents"): {"page_size": 1},
        ("CommonService", "Diagnose"): {
            "target_id": "gateway",
            "stages": ["configuration"],
            "timeout_ms": 1000,
            "correlation_id": "e2e-surface-matrix",
        },
        # Collector valid discovery/read operations.
        ("CollectorService", "GetDevice"): {"device_id": "WTG_SIM01"},
        ("CollectorService", "ListPoints"): {"device_id": "WTG_SIM01", "page_size": 2},
        ("CollectorService", "GetPoint"): {
            "device_id": "WTG_SIM01",
            "point_id": "wind_speed",
        },
        ("CollectorService", "ReadPoints"): {
            "device_id": "WTG_SIM01",
            "point_ids": ["wind_speed"],
        },
        ("CollectorService", "ListTasks"): {"page_size": 2},
        ("CollectorService", "GetTask"): {"id": "missing-e2e-matrix-task"},
        ("CollectorService", "GetPublishingStatus"): {},
        ("CollectorService", "InterrogateDevice"): {"device_id": "WTG_SIM01"},
        # Commander discovery is valid. Mutations and result queries use missing IDs.
        ("CommanderService", "RecoverDeviceControl"): {
            "device_id": "missing-e2e-device",
            "expected_revision": "missing-revision",
            "request_id": "e2e-matrix-recovery",
            "isolation_attestation": "ZTI=",
        },
        ("CommanderService", "ListDevices"): {"page_size": 2},
        ("CommanderService", "GetDevice"): {"id": "PPC_SIM01"},
        ("CommanderService", "ListPoints"): {"device_id": "PPC_SIM01", "page_size": 2},
        ("CommanderService", "GetCommandResult"): {"command_id": "missing-e2e-command"},
        ("CommanderService", "GetRequestResult"): {"request_id": "missing-e2e-request"},
        # Sink valid discovery plus harmless missing-object operations.
        ("SinkService", "ListDeliveries"): {"page_size": 2},
        ("SinkService", "GetDelivery"): {"id": "ems_power"},
        # Gateway valid discovery/read plus harmless invalid mutations.
        ("IndustrialGatewayService", "ListGateways"): {"page_size": 5},
        ("IndustrialGatewayService", "GetGateway"): {"id": "gw-wtg_sim01"},
        ("IndustrialGatewayService", "ListVariables"): {
            "gateway_id": "gw-wtg_sim01",
            "page_size": 2,
        },
        ("IndustrialGatewayService", "ReadVariables"): {
            "gateway_id": "gw-wtg_sim01",
            "variable_ids": ["wind_speed"],
        },
        ("IndustrialGatewayService", "GetSimulationExecutionResult"): {
            "gateway_id": "gw-ppc_sim01",
            "request_id": "missing-e2e-execution",
        },
        # Generator valid discovery plus missing generation/execution identities.
        ("DataGeneratorService", "ListDevices"): {"page_size": 2},
        ("DataGeneratorService", "GetDevice"): {"id": "WTG_SIM01"},
        ("DataGeneratorService", "ListScenarios"): {"page_size": 2},
        ("DataGeneratorService", "GetGeneration"): {"id": "missing-e2e-generation"},
        ("DataGeneratorService", "ListVariables"): {
            "generation_id": "missing-e2e-generation",
            "device_id": "WTG_SIM01",
            "page_size": 2,
        },
        ("DataGeneratorService", "GetInputExecution"): {
            "generation_id": "missing-e2e-generation",
            "request_id": "missing-e2e-input",
        },
    }


@pytest.mark.timeout(180)
async def test_e2e_08_executes_all_51_formal_rpcs(
    full_chain: FullChainHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute exactly the current descriptor denominator over authenticated gRPC."""
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", ADMIN_TOKEN)
    runtime = Runtime(full_chain.cli_rt, "json")
    values = _requests(full_chain)
    evidence: list[dict[str, Any]] = []
    forbidden = {
        grpc.StatusCode.CANCELLED,
        grpc.StatusCode.UNKNOWN,
        grpc.StatusCode.DEADLINE_EXCEEDED,
        grpc.StatusCode.UNIMPLEMENTED,
        grpc.StatusCode.INTERNAL,
        grpc.StatusCode.UNAVAILABLE,
        grpc.StatusCode.DATA_LOSS,
        grpc.StatusCode.UNAUTHENTICATED,
    }

    try:
        for module_name, service_name, service_key in RPC_SERVICES:
            pb2 = importlib.import_module(f"contracts.{module_name}_pb2")
            pb2_grpc = importlib.import_module(f"contracts.{module_name}_pb2_grpc")
            descriptor = pb2.DESCRIPTOR.services_by_name[service_name]
            stub = getattr(pb2_grpc, f"{service_name}Stub")(runtime.channel(service_key))
            for method in descriptor.methods:
                fields = values.get((service_name, method.name), {})
                request = ParseDict(
                    fields,
                    message_factory.GetMessageClass(method.input_type)(),
                    ignore_unknown_fields=False,
                )
                record: dict[str, Any] = {
                    "service": service_name,
                    "method": method.name,
                    "request_type": method.input_type.full_name,
                    "response_type": method.output_type.full_name,
                    "input": fields,
                }
                try:
                    response = await getattr(stub, method.name)(
                        request,
                        timeout=runtime.target(service_key).timeout,
                        metadata=runtime.get_rpc_metadata(service_key, method.name),
                    )
                    record["observed_status"] = "OK"
                    record["response"] = MessageToDict(
                        response,
                        preserving_proto_field_name=True,
                        always_print_fields_with_no_presence=True,
                    )
                except grpc.aio.AioRpcError as exc:
                    record["observed_status"] = exc.code().name
                    record["details"] = exc.details()
                    assert exc.code() not in forbidden, json.dumps(record, ensure_ascii=False)
                evidence.append(record)
    finally:
        await runtime.close()

    names = {(item["service"], item["method"]) for item in evidence}
    assert len(evidence) == 51, json.dumps(evidence, ensure_ascii=False)
    assert len(names) == 51
    full_chain.record("E2E-08-RPC", total=51, executed=51, calls=evidence)


def _last_cli_call(log: Path) -> dict[str, Any]:
    rows = [line for line in log.read_text(encoding="utf-8").splitlines() if line]
    assert rows, f"CLI call log is empty: {log}"
    return json.loads(rows[-1])


@pytest.mark.timeout(600)
async def test_e2e_08_executes_all_73_cli_leaves(full_chain: FullChainHarness) -> None:
    """Execute the runtime-enumerated CLI denominator, preserving service state."""
    calls: list[dict[str, Any]] = []

    async def execute(command_id: str, *args: str, timeout: float = 45) -> dict[str, Any]:
        await full_chain.cli_call(
            full_chain.cli_rt,
            *args,
            timeout=timeout,
            check=False,
        )
        observed = _last_cli_call(full_chain.cli.log)
        output = f"{observed.get('stdout', '')}\n{observed.get('stderr', '')}"
        # A parser/help rejection does not count as executing the leaf command.
        assert "No such command" not in output, output
        assert "Missing argument" not in output, output
        assert "Missing option" not in output, output
        assert "Usage:" not in output, output
        assert observed["returncode"] in {0, 1, 2, 3}, observed
        observed["command_id"] = command_id
        calls.append(observed)
        return observed

    # Local, service and configuration leaves.
    await execute("help", "help")
    await execute("exit", "exit")
    await execute("service list", "service", "list")
    await execute("service status", "service", "status")
    await execute("service info", "service", "info", "gateway")
    await execute("service capabilities", "service", "capabilities", "gateway")
    await execute("service start", "service", "start", "gateway", "--yes")
    await execute(
        "service restart",
        "service",
        "restart",
        "collector_reliable",
        "--yes",
        timeout=75,
    )
    await execute("service stop", "service", "stop", "collector_reliable", "--yes")
    # Restore the process immediately; this restoration is not an additional matrix row.
    restored = await full_chain.service("start", "collector_reliable")
    assert restored.get("data", {}).get("process", {}).get("confirmed") is True, restored
    await full_chain.poll(
        full_chain.cli_rel,
        "collector",
        "devices",
        predicate=lambda data: bool(data.get("devices")),
        timeout=30,
    )

    left = full_chain.root / "matrix-diff-left"
    right = full_chain.root / "matrix-diff-right"
    empty_snapshot = full_chain.root / "matrix-empty-snapshot"
    left.mkdir(exist_ok=True)
    right.mkdir(exist_ok=True)
    empty_snapshot.mkdir(exist_ok=True)
    (empty_snapshot / "snapshot_probe.yaml").write_text("probe: true\n", encoding="utf-8")
    await execute("config diff", "config", "diff", str(left), str(right))
    await execute("config get", "config", "get", "gateway", "--scope", "disk")
    await execute(
        "config push",
        "config",
        "push",
        "gateway",
        str(empty_snapshot),
        "--expected-revision",
        "deliberately-stale-e2e-revision",
    )
    await execute(
        "config apply",
        "config",
        "apply",
        "gateway",
        "missing-e2e-candidate",
        "--expected-revision",
        "deliberately-stale-e2e-revision",
        "--yes",
    )
    await execute("net ping", "net", "ping", "127.0.0.1", "--timeout", "1")
    await execute(
        "net tcp",
        "net",
        "tcp",
        "127.0.0.1",
        str(full_chain.ports["gateway"]),
        "--timeout",
        "1",
    )
    await execute("net industrial", "net", "industrial", "127.0.0.1", "--timeout", "0.1")
    await execute("net scan", "net", "scan", "127.0.0.1/32", "--timeout", "0.1")

    # Gateway discovery, lifecycle and a safe authorization rejection.
    await execute("gateway list", "gateway", "list", "--page-size", "5")
    await execute("gateway status", "gateway", "status", "gw-wtg_sim01")
    await execute("gateway variables", "gateway", "variables", "gw-wtg_sim01")
    await execute("gateway read", "gateway", "read", "gw-wtg_sim01", "wind_speed")
    await execute("gateway metrics", "gateway", "metrics", "--scope", "e2e", "--window-ms", "1000")
    await execute("gateway events", "gateway", "events", "--page-size", "1")
    await execute(
        "gateway update",
        "gateway",
        "update",
        "gw-wtg_sim01",
        "missing_e2e_variable=1",
        "--owner-id",
        "cli",
        "--yes",
    )
    await execute("gateway stop", "gateway", "stop", "gw-wtg_sim01", "--yes")
    await execute("gateway start", "gateway", "start", "gw-wtg_sim01", "--yes")

    # Collector: keep a real task active while read/watch/diagnose are exercised.
    tasks_call = await execute("collector tasks", "collector", "tasks")
    tasks_envelope = json.loads(tasks_call["stdout"].strip().splitlines()[-1])
    tasks = tasks_envelope.get("data", {}).get("tasks", [])
    assert tasks, tasks_envelope
    task_id = tasks[0]["task_id"]
    await execute("collector devices", "collector", "devices")
    await execute("collector device", "collector", "device", "WTG_SIM01")
    await execute("collector points", "collector", "points", "WTG_SIM01")
    await execute("collector point", "collector", "point", "WTG_SIM01", "wind_speed")
    await execute("collector task", "collector", "task", task_id)
    await execute("collector start", "collector", "start", task_id, "--yes")
    await execute("collector latest", "collector", "latest", "WTG_SIM01", "wind_speed")
    await execute(
        "collector watch",
        "collector",
        "watch",
        "WTG_SIM01",
        "wind_speed",
        "--interval",
        "0.1",
        "--count",
        "1",
    )
    await execute("collector publishing", "collector", "publishing")
    await execute(
        "collector metrics",
        "collector",
        "metrics",
        "--scope",
        "e2e",
        "--window-ms",
        "1000",
    )
    await execute("collector events", "collector", "events", "--page-size", "1")

    # Sink discovery and a real stop/start transition, restored to START.
    await execute("sink deliveries", "sink", "deliveries")
    await execute("sink delivery", "sink", "delivery", "ems_power")
    await execute("sink metrics", "sink", "metrics", "--scope", "e2e", "--window-ms", "1000")
    await execute("sink events", "sink", "events", "--page-size", "1")
    await execute("sink stop", "sink", "stop", "ems_power", "--yes")
    await execute("sink start", "sink", "start", "ems_power", "--yes")

    # Start one real generation so all Generator leaves and diagnostics have a
    # legitimate running identity.  The finally block preserves fixture cleanup.
    generation_id = ""
    try:
        await execute("data-generator devices", "data-generator", "devices")
        await execute("data-generator device", "data-generator", "device", "WTG_SIM01")
        await execute("data-generator scenarios", "data-generator", "scenarios")
        await execute(
            "data-generator metrics",
            "data-generator",
            "metrics",
            "--scope",
            "e2e",
            "--window-ms",
            "1000",
        )
        await execute("data-generator events", "data-generator", "events", "--page-size", "1")
        started_call = await execute(
            "data-generator start",
            "data-generator",
            "start",
            "wind-plant-physical",
            "--yes",
        )
        started = json.loads(started_call["stdout"].strip().splitlines()[-1])
        generation_id = str(started.get("data", {}).get("generation_id", ""))
        assert generation_id, started
        full_chain.generation_id = generation_id
        await execute("data-generator status", "data-generator", "status", generation_id)
        await execute(
            "data-generator variables list",
            "data-generator",
            "variables",
            "list",
            "--generation-id",
            generation_id,
            "--device-id",
            "WTG_SIM01",
        )
        await execute(
            "data-generator variables update",
            "data-generator",
            "variables",
            "update",
            "--generation-id",
            generation_id,
            "--device-id",
            "WTG_SIM01",
            "--request-id",
            f"e2e-matrix-{uuid4()}",
            "--point-id",
            "wind_speed_mean",
            "--value",
            "10",
            "--yes",
        )

        # Commander valid discovery plus safe missing-identity rejection paths.
        await execute("commander devices", "commander", "devices")
        await execute("commander device", "commander", "device", "PPC_SIM01")
        await execute("commander points", "commander", "points", "PPC_SIM01")
        await execute("commander result", "commander", "result", "missing-e2e-command")
        await execute(
            "commander request-result",
            "commander",
            "request-result",
            "missing-e2e-request",
        )
        attestation = full_chain.root / "matrix-attestation.bin"
        attestation.write_bytes(b"isolated-e2e-evidence")
        await execute(
            "commander recover-device",
            "commander",
            "recover-device",
            "--device-id",
            "missing-e2e-device",
            "--expected-revision",
            "missing-e2e-revision",
            "--request-id",
            f"e2e-recover-{uuid4()}",
            "--attestation-file",
            str(attestation),
        )
        expiry = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
        await execute(
            "commander write",
            "commander",
            "write",
            "missing-e2e-device",
            "missing_e2e_point",
            "1",
            "--expected-revision",
            "missing-e2e-revision",
            "--expires-at",
            expiry,
            "--request-id",
            f"e2e-write-{uuid4()}",
            "--yes",
        )
        batch = full_chain.root / "matrix-command-batch.json"
        batch.write_text(
            json.dumps(
                [
                    {
                        "device_id": "missing-e2e-device",
                        "point_id": "missing_e2e_point",
                        "value": 1,
                        "expected_revision": "missing-e2e-revision",
                        "expires_at": expiry,
                    }
                ]
            ),
            encoding="utf-8",
        )
        await execute("commander batch", "commander", "batch", str(batch), "--yes")
        await execute(
            "commander metrics",
            "commander",
            "metrics",
            "--scope",
            "e2e",
            "--window-ms",
            "1000",
        )
        await execute("commander events", "commander", "events", "--page-size", "1")

        # All diagnostic leaves execute against the same live identities.
        await execute("diagnose collector device", "diagnose", "collector", "device", "WTG_SIM01")
        await execute(
            "diagnose collector pipeline", "diagnose", "collector", "pipeline", "WTG_SIM01"
        )
        await execute("diagnose collector gi", "diagnose", "collector", "gi", "WTG_SIM01")
        await execute("diagnose commander", "diagnose", "commander", "PPC_SIM01")
        await execute("diagnose sink", "diagnose", "sink", "ems_power")
        await execute("diagnose gateway", "diagnose", "gateway", "gw-wtg_sim01")
        await execute("diagnose data-generator", "diagnose", "data-generator", generation_id)
        await execute(
            "diagnose pipeline",
            "diagnose",
            "pipeline",
            "WTG_SIM01",
            "--sink-id",
            "ems_power",
        )
    finally:
        if generation_id:
            await execute(
                "data-generator stop",
                "data-generator",
                "stop",
                generation_id,
                "--yes",
            )
            full_chain.generation_id = None
        await execute("collector stop", "collector", "stop", task_id, "--yes")

    expected = set(_command_words())
    executed = {item["command_id"] for item in calls}
    assert expected == executed, {
        "missing": sorted(expected - executed),
        "unexpected": sorted(executed - expected),
    }
    assert len(calls) == len(expected) == 73
    full_chain.record("E2E-08-CLI", total=73, executed=73, calls=calls)
