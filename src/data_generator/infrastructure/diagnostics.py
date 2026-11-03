"""Read-only evidence-based diagnostics of the Generator to Gateway data path."""

from __future__ import annotations

import asyncio
import csv
import time
from datetime import datetime, timezone

import grpc
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as gateway
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from core.infrastructure.rpc.scalar_codec import encode_scalar
from data_generator.application.models import generate_values
from data_generator.domain.scenario import Scenario, Settings, resolve_history


def _timestamp() -> Timestamp:
    result = Timestamp()
    result.FromDatetime(datetime.now(timezone.utc))
    return result


class Diagnostics:
    """Diagnose does not mutate the active PRNG, Gateway variables or source cursors."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        options = settings.service.get("diagnose", {})
        self._max_seconds = float(options.get("timeout_seconds", 10.0))

    def _scenario(self, target_id: str) -> Scenario | None:
        direct = self.settings.scenarios.get(target_id)
        if direct is not None:
            return direct
        candidates = [
            item for item in self.settings.scenarios.values()
            if target_id in item.gateways
        ]
        return candidates[0] if len(candidates) == 1 else None

    async def _grpc_check(
        self, scenario: Scenario, gateway_id: str, endpoint: str, timeout: float,
    ) -> int:
        expected = {
            mapping.target.variable_id: scenario.points[
                (mapping.device_id, mapping.point_id)
            ]["data_type"]
            for mapping in scenario.mappings
            if mapping.target.gateway_id == gateway_id
        }
        actual: dict[str, tuple[str, bool]] = {}
        async with grpc.aio.insecure_channel(endpoint) as channel:
            stub = gateway_rpc.IndustrialGatewayServiceStub(channel)
            token = ""
            for _ in range(100):
                response = await asyncio.wait_for(
                    stub.ListVariables(gateway.ListVariablesRequest(
                        gateway_id=gateway_id, page_size=1000, page_token=token,
                    )),
                    timeout,
                )
                for item in response.variables:
                    if item.variable_id in actual:
                        raise ValueError(f"duplicate Gateway variable: {item.variable_id}")
                    actual[item.variable_id] = (item.data_type, item.writable)
                token = response.next_page_token
                if not token:
                    break
            else:
                raise ValueError("Gateway variable catalog page limit exceeded")
        missing = set(expected) - set(actual)
        if missing:
            raise ValueError(f"missing Gateway variable(s): {sorted(missing)}")
        incompatible = [
            variable_id for variable_id, data_type in expected.items()
            if actual[variable_id] != (data_type, True)
        ]
        if incompatible:
            raise ValueError(f"Gateway scalar type mismatch: {sorted(incompatible)}")
        return len(expected)

    async def run(self, request: common.DiagnoseRequest) -> common.DiagnoseResponse:
        scenario = self._scenario(request.target_id)
        if scenario is None:
            return common.DiagnoseResponse(
                overall_status="UNKNOWN", limitations=["NO_DIAGNOSTIC_TARGETS"],
            )
        selected = list(request.stages or ("source", "mapping", "network", "grpc", "write"))
        aliases = {"D1": "source", "D2": "mapping", "D3": "network",
                   "D4": "grpc", "D5": "write"}
        stages = [aliases.get(stage, stage) for stage in selected]
        if len(stages) > 10 or len(stages) != len(set(stages)):
            return common.DiagnoseResponse(
                overall_status="FAIL", limitations=["duplicate or excessive stages"],
            )
        timeout = min(
            request.timeout_ms / 1000.0 if request.timeout_ms else self._max_seconds,
            self._max_seconds,
        )
        timeout = max(timeout, 0.1)
        output: list[common.DiagnosticStage] = []
        limitations: list[str] = []
        for name in stages:
            began = time.monotonic()
            status, error_code, message = "SKIPPED", "NOT_VERIFIED", ""
            evidence: list[common.Evidence] = []
            try:
                if name == "source":
                    if scenario.source["kind"] == "replay":
                        path = resolve_history(self.settings.root, scenario.source["file"])
                        def check_csv() -> int:
                            with path.open("r", encoding="utf-8", newline="") as stream:
                                header = tuple(next(csv.reader(stream)))
                            if header != (
                                "device_id", "point_id", "source_timestamp",
                                "value", "quality", "record_id",
                            ):
                                raise ValueError("replay CSV header mismatch")
                            return path.stat().st_size
                        length = await asyncio.wait_for(asyncio.to_thread(check_csv), timeout)
                        evidence.append(common.Evidence(
                            name="history_bytes", value=str(length),
                        ))
                        limitations.append("full CSV contents are checked during replay startup")
                    else:
                        generated = generate_values(
                            int(scenario.source.get("seed", 0)), 1, scenario.points,
                        )
                        evidence.append(common.Evidence(
                            name="generated_points", value=str(len(generated)),
                        ))
                    status, error_code = "PASS", ""
                elif name == "mapping":
                    if not scenario.mappings:
                        raise ValueError("no output mappings")
                    targets = [mapping.target for mapping in scenario.mappings]
                    if len(targets) != len(set(targets)):
                        raise ValueError("duplicate output target")
                    if scenario.source["kind"] == "simulation":
                        values = generate_values(
                            int(scenario.source.get("seed", 0)), 1, scenario.points,
                        )
                        for value in values.values():
                            if encode_scalar(value).WhichOneof("kind") is None:
                                raise ValueError("invalid scalar wire encoding")
                        evidence.append(common.Evidence(
                            name="encoded_points", value=str(len(values)),
                        ))
                    else:
                        limitations.append(
                            "replay scalar contents checked during source initialization"
                        )
                    evidence.append(common.Evidence(
                        name="unique_targets", value=str(len(targets)),
                    ))
                    status, error_code = "PASS", ""
                elif name == "network":
                    for gateway_id, endpoint in scenario.gateways.items():
                        host, port = endpoint.rsplit(":", 1)
                        host = host.removeprefix("[").removesuffix("]")
                        _, writer = await asyncio.wait_for(
                            asyncio.open_connection(host, int(port)), timeout,
                        )
                        writer.close()
                        await writer.wait_closed()
                        evidence.append(common.Evidence(
                            name="reachable_gateway", value=gateway_id,
                        ))
                    status, error_code = "PASS", ""
                elif name == "grpc":
                    for gateway_id, endpoint in scenario.gateways.items():
                        count = await self._grpc_check(scenario, gateway_id, endpoint, timeout)
                        evidence.append(common.Evidence(
                            name=f"{gateway_id}.validated_variables", value=str(count),
                        ))
                    status, error_code = "PASS", ""
                elif name == "write":
                    limitations.append("active write skipped: no authorized isolated test target")
                else:
                    status, error_code = "FAIL", "UNKNOWN_STAGE"
                    message = f"unsupported diagnostic stage: {name}"
            except (OSError, ValueError, TimeoutError, StopIteration, grpc.RpcError) as exc:
                status, error_code, message = "FAIL", "DIAGNOSTIC_FAILED", str(exc)
            duration = max(0, round((time.monotonic() - began) * 1000))
            output.append(common.DiagnosticStage(
                stage=name, status=status, error_code=error_code, message=message,
                duration_ms=duration, observed_at=_timestamp(),
                source="data_generator", evidence=evidence,
            ))
        overall = ("FAIL" if any(item.status == "FAIL" for item in output)
                   else "UNKNOWN" if any(item.status == "SKIPPED" for item in output)
                   else "PASS")
        return common.DiagnoseResponse(
            overall_status=overall, stages=output, limitations=limitations,
        )
