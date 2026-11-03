"""Read-only staged diagnostics of the Generator-to-Gateway data path."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any

import grpc

from contracts import common_pb2 as common
from contracts import industrial_gateway_pb2 as gateway
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from data_generator.domain.scenario import Settings, resolve_history


class Diagnostics:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def run(self, request: common.DiagnoseRequest) -> common.DiagnoseResponse:
        scenario = self.settings.scenarios.get(request.target_id)
        if scenario is None:
            return common.DiagnoseResponse(
                overall_status="UNKNOWN", limitations=["NO_DIAGNOSTIC_TARGETS"],
            )
        stages = list(request.stages or ("source", "mapping", "network", "grpc", "write"))
        if len(stages) > 10 or len(stages) != len(set(stages)):
            return common.DiagnoseResponse(
                overall_status="FAIL", limitations=["invalid or duplicate diagnostic stages"],
            )
        timeout = min(max(request.timeout_ms / 1000.0, 0.1), 30.0)
        output = []
        limitations: list[str] = []
        for name in stages:
            started = time.monotonic()
            status, code, message = "SKIPPED", "NOT_VERIFIED", ""
            evidence: list[common.Evidence] = []
            try:
                if name == "source":
                    if scenario.source["kind"] == "replay":
                        file_path = resolve_history(self.settings.root, scenario.source["file"])
                        await asyncio.wait_for(asyncio.to_thread(file_path.stat), timeout)
                        evidence.append(common.Evidence(name="history", value="accessible"))
                    else:
                        evidence.append(common.Evidence(name="model", value="configured"))
                    status, code = "PASS", ""
                elif name == "mapping":
                    if not scenario.mappings:
                        raise ValueError("no output mappings")
                    evidence.append(common.Evidence(
                        name="targets", value=str(len(scenario.mappings)),
                    ))
                    status, code = "PASS", ""
                elif name in ("network", "grpc"):
                    for gateway_id, endpoint in scenario.gateways.items():
                        if name == "network":
                            host, port = endpoint.rsplit(":", 1)
                            reader, writer = await asyncio.wait_for(
                                asyncio.open_connection(host, int(port)), timeout,
                            )
                            writer.close()
                            await writer.wait_closed()
                        else:
                            async with grpc.aio.insecure_channel(endpoint) as channel:
                                stub = gateway_rpc.IndustrialGatewayServiceStub(channel)
                                await asyncio.wait_for(stub.ListVariables(
                                    gateway.ListVariablesRequest(gateway_id=gateway_id, page_size=1),
                                ), timeout)
                        evidence.append(common.Evidence(name="gateway_id", value=gateway_id))
                    status, code = "PASS", ""
                elif name == "write":
                    limitations.append("write not executed: no isolated authorized test target")
                else:
                    status, code = "FAIL", "UNKNOWN_STAGE"
                    message = f"unsupported diagnostic stage: {name}"
            except (OSError, ValueError, TimeoutError, grpc.RpcError) as exc:
                status, code, message = "FAIL", "DIAGNOSTIC_FAILED", str(exc)
            duration = max(0, round((time.monotonic() - started) * 1000))
            output.append(common.DiagnosticStage(
                stage=name, status=status, error_code=code, message=message,
                duration_ms=duration, observed_at=self._timestamp(),
                source="data_generator", evidence=evidence,
            ))
        overall = ("FAIL" if any(s.status == "FAIL" for s in output)
                   else "UNKNOWN" if any(s.status == "SKIPPED" for s in output)
                   else "PASS")
        return common.DiagnoseResponse(
            overall_status=overall, stages=output, limitations=limitations,
        )

    @staticmethod
    def _timestamp() -> Any:
        from google.protobuf.timestamp_pb2 import Timestamp

        result = Timestamp()
        result.FromDatetime(datetime.now(timezone.utc))
        return result
