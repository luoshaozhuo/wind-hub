"""Diagnose individual service stages and cross-service delivery evidence."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import typer

from .common_commands import request_common

diagnose = typer.Typer(help="Stage-based diagnostics, not merely TCP availability.")
collector = typer.Typer(help="Collector device, publishing and IEC104 GI checks.")
diagnose.add_typer(collector, name="collector")


def _save_evidence(output: Path, data: dict[str, Any]) -> None:
    """Write a private JSON evidence file exactly once; never overwrite evidence."""
    if output.suffix != ".json" or not output.parent.is_dir() or output.is_symlink():
        raise ValueError("--output must name a new .json file in an existing directory")
    payload = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _single_diagnose(
    service: str,
    target: str,
    output: Path | None,
    stages: list[str] | None = None,
) -> None:
    from .main import _run

    async def probe() -> dict[str, Any]:
        result = await request_common(
            service, "Diagnose", {"target_id": target, "stages": stages or []}
        )
        if output is not None:
            await asyncio.to_thread(_save_evidence, output, result)
        return result

    _run(f"diagnose {service}", probe())


@collector.command("device")
def collector_device(device_id: str, output: Path | None = typer.Option(None, "--output")) -> None:
    _single_diagnose("collector", device_id, output)


@collector.command("pipeline")
def collector_pipeline(
    device_id: str, output: Path | None = typer.Option(None, "--output")
) -> None:
    _single_diagnose(
        "collector",
        device_id,
        output,
        stages=["NETWORK", "PROTOCOL", "ACQUISITION", "NATS", "BOUNDARY"],
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
def commander_diagnose(
    device_id: str,
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    _single_diagnose("commander", device_id, output)


@diagnose.command("sink")
def sink_diagnose(
    delivery_id: str,
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    _single_diagnose("sink", delivery_id, output)


@diagnose.command("gateway")
def gateway_diagnose(
    gateway_id: str,
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    _single_diagnose("gateway", gateway_id, output)


@diagnose.command("data-generator")
def generator_diagnose(
    generation_id: str,
    output: Path | None = typer.Option(None, "--output"),
) -> None:
    _single_diagnose("data-generator", generation_id, output)


@diagnose.command("pipeline")
def pipeline_diagnose(
    device_id: str,
    sink_id: str = typer.Option(..., "--sink-id", help="Exact Sink Delivery ID"),
    output: Path | None = typer.Option(None, "--output"),
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
        if output is not None:
            await asyncio.to_thread(_save_evidence, output, reports)
        return reports

    _run("diagnose pipeline", probe())
