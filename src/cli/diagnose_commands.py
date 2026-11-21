"""Diagnose individual service stages and cross-service delivery evidence."""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import typer

from .common_commands import _run_common

diagnose = typer.Typer(help="Stage-based diagnostics, not merely TCP availability.")
collector = typer.Typer(help="Collector device, publishing and IEC104 GI checks.")
diagnose.add_typer(collector, name="collector")


@collector.command("device")
def collector_device(device_id: str) -> None:
    _run_common("collector", "Diagnose", {"target_id": device_id, "stages": []})


@collector.command("pipeline")
def collector_pipeline(device_id: str) -> None:
    _run_common(
        "collector",
        "Diagnose",
        {
            "target_id": device_id,
            "stages": ["NETWORK", "PROTOCOL", "ACQUISITION", "NATS", "BOUNDARY"],
        },
    )


@collector.command("gi")
def collector_gi(
    device_id: str,
    ca: int | None = typer.Option(None, "--ca"),
    timeout_ms: int = typer.Option(5000, "--timeout-ms"),
) -> None:
    from .main import _run, runtime
    from .service_rpc import invoke

    async def interrogate() -> dict[str, Any]:
        # 参数越界必须在 _run 协程内抛出，才能走统一信封/退出码 2，而非裸 traceback。
        if ca is not None and not 0 <= ca <= 65535:
            raise ValueError("IEC104 common address must be 0..65535")
        if not 1 <= timeout_ms <= 120000:
            raise ValueError("timeout-ms must be in 1..120000")
        fields: dict[str, Any] = {"device_id": device_id, "timeout_ms": timeout_ms}
        if ca is not None:
            fields["common_address"] = ca
        return await invoke(runtime(), "collector", "InterrogateDevice", fields)

    _run("collector gi", interrogate())


@diagnose.command("commander")
def commander_diagnose(device_id: str) -> None:
    _run_common("commander", "Diagnose", {"target_id": device_id})


@diagnose.command("sink")
def sink_diagnose(delivery_id: str) -> None:
    from .plant_commands import _run_sink_common

    _run_sink_common("Diagnose", {"target_id": delivery_id})


@diagnose.command("gateway")
def gateway_diagnose(gateway_id: str) -> None:
    _run_common("gateway", "Diagnose", {"target_id": gateway_id})


@diagnose.command("data-generator")
def generator_diagnose(generation_id: str) -> None:
    _run_common("data-generator", "Diagnose", {"target_id": generation_id})


@diagnose.command("pipeline")
def pipeline_diagnose(
    device_id: str,
    sink_id: str = typer.Option(..., "--sink-id", help="Exact Sink Delivery ID"),
) -> None:
    """Never claim end-to-end PASS without correlated evidence from both services."""
    from .main import _run, runtime

    async def probe() -> dict[str, Any]:
        from google.protobuf.json_format import MessageToDict

        from contracts import common_pb2, common_pb2_grpc

        reports: dict[str, Any] = {}
        correlation_id = str(uuid4())
        for svc_name, stages in (
            ("collector", ["NETWORK", "PROTOCOL", "ACQUISITION", "NATS", "BOUNDARY"]),
            ("sink", ["source", "consumer", "delivery"]),
        ):
            target = device_id if svc_name == "collector" else sink_id
            rpc_target = svc_name
            stub = common_pb2_grpc.CommonServiceStub(runtime().channel(rpc_target))
            # Both management calls must attach their scoped credentials: the
            # Collector admin token or the Sink Bearer token from get_rpc_metadata.
            metadata = (
                runtime().collector_admin_metadata()
                if svc_name == "collector"
                else runtime().get_rpc_metadata(rpc_target, "Diagnose")
            )
            response = await stub.Diagnose(
                common_pb2.DiagnoseRequest(
                    target_id=target, stages=stages, correlation_id=correlation_id
                ),
                timeout=runtime().target(rpc_target).timeout,
                metadata=metadata,
            )
            reports[svc_name] = MessageToDict(
                response,
                preserving_proto_field_name=True,
                always_print_fields_with_no_presence=True,
            )

        def verified_sample_ids(report: dict[str, Any]) -> set[str]:
            """Only device-bound structured evidence can prove pipeline delivery."""
            verified: set[str] = set()
            for stage in report.get("stages", []):
                if not isinstance(stage, dict):
                    continue
                for evidence in stage.get("evidence", []):
                    if not isinstance(evidence, dict) or evidence.get("name") != "sample":
                        continue
                    value = evidence.get("value")
                    if not isinstance(value, str):
                        continue
                    try:
                        item = json.loads(value)
                    except (ValueError, TypeError):
                        continue
                    if (
                        isinstance(item, dict)
                        and item.get("device_id") == device_id
                        and isinstance(item.get("sample_id"), str)
                        and item["sample_id"]
                    ):
                        verified.add(item["sample_id"])
            return verified

        collector_report = reports["collector"]
        sink_report = reports["sink"]
        states = {collector_report.get("overall_status"), sink_report.get("overall_status")}
        if states & {"FAIL", "FAILED", "FAILURE"}:
            reports["overall_status"] = "FAIL"
        elif states <= {"PASS", "SUCCESS"} and (
            verified_sample_ids(collector_report) & verified_sample_ids(sink_report)
        ):
            reports["overall_status"] = "PASS"
        else:
            reports["overall_status"] = "UNKNOWN"
            reports["limitations"] = [
                "No shared, verified message/sample ID across Collector and Sink.",
            ]
        return reports

    _run("diagnose pipeline", probe())
