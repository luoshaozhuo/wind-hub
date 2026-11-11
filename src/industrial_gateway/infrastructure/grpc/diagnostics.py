"""Side-effect-free evidence collection for Gateway Diagnose RPC."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from time import perf_counter

import grpc

from contracts import common_pb2 as pb
from industrial_gateway.infrastructure.commander.forwarder import ForwardState
from industrial_gateway.infrastructure.runtime import GatewayRuntime

_STAGES = ("injection", "protocol", "control")


class GatewayDiagnostics:
    """Inspect actual state; never synthesize PLC writes or Commander commands."""

    def __init__(self, runtime: GatewayRuntime) -> None:
        self._runtime = runtime
        self._capacity = asyncio.Semaphore(4)

    async def diagnose(self, request: pb.DiagnoseRequest) -> pb.DiagnoseResponse:
        """Return observed evidence only, with an explicit uncertainty boundary."""
        if request.target_id and request.target_id not in self._runtime.instances:
            raise KeyError("gateway target not found")
        stages = tuple(request.stages) or _STAGES
        if len(stages) != len(set(stages)) or any(stage not in _STAGES for stage in stages):
            raise ValueError("unknown or duplicate diagnostic stage")
        timeout = min((request.timeout_ms or 5000) / 1000, 5.0)
        try:
            async with asyncio.timeout(timeout):
                async with self._capacity:
                    return await self._run(stages, request.target_id)
        except TimeoutError:
            return pb.DiagnoseResponse(
                overall_status="UNKNOWN",
                limitations=["diagnosis deadline reached before completion"],
            )

    async def _run(self, stages: tuple[str, ...], target: str) -> pb.DiagnoseResponse:
        observed: list[pb.DiagnosticStage] = []
        limitations: list[str] = []
        targets = {target: self._runtime.instances[target]} if target else self._runtime.instances
        for name in stages:
            start = perf_counter()
            status = "UNKNOWN"
            evidence: list[pb.Evidence] = []
            error_code = ""
            message = ""
            if name == "injection":
                evidence.append(
                    pb.Evidence(
                        name="registered_gateways",
                        value=str(len(targets)),
                    )
                )
                evidence.append(
                    pb.Evidence(
                        name="registered_variables",
                        value=str(sum(len(item.config.variables) for item in targets.values())),
                    )
                )
                # Configuration presence is not proof of a successful update.
                status = "UNKNOWN"
                limitations.append("registration alone cannot prove an upstream gRPC write/read")
            elif name == "protocol":
                failures = []
                for gateway_id, item in targets.items():
                    state = item.lifecycle.status
                    serving = item.protocol_server is not None and item.protocol_server.is_serving
                    evidence.append(
                        pb.Evidence(
                            name=f"{gateway_id}.state",
                            value=state.actual.value,
                        )
                    )
                    evidence.append(
                        pb.Evidence(
                            name=f"{gateway_id}.listener",
                            value=str(serving).lower(),
                        )
                    )
                    if state.desired.value == "START" and not serving:
                        failures.append(gateway_id)
                if failures:
                    status = "FAIL"
                    error_code = "LISTENER_NOT_SERVING"
                    message = ",".join(failures)
                else:
                    status = "UNKNOWN"
                    limitations.append(
                        "listener presence cannot prove external protocol read/write"
                    )
            elif name == "control":
                mapped = sum(
                    len(item.config.settings.command_mappings)
                    for item in targets.values()
                    if item.config.settings.control_enabled
                )
                evidence.append(pb.Evidence(name="configured_mappings", value=str(mapped)))
                evidence.append(
                    pb.Evidence(
                        name="forward_events_pending",
                        value=str(self._runtime.forwarder.pending),
                    )
                )
                if mapped:
                    endpoint = self._runtime.config.service.control.commander_endpoint
                    host, separator, port_text = endpoint.rpartition(":")
                    if not separator:
                        status = "UNKNOWN"
                        limitations.append("Commander endpoint is not host:port")
                    else:
                        try:
                            connector = asyncio.open_connection(host.strip("[]"), int(port_text))
                            reader, writer = await asyncio.wait_for(connector, timeout=0.5)
                            del reader
                            writer.close()
                            await writer.wait_closed()
                        except (OSError, ValueError, TimeoutError) as exc:
                            status = "FAIL"
                            error_code = "COMMANDER_TCP_UNREACHABLE"
                            message = str(exc)
                            evidence.append(
                                pb.Evidence(
                                    name="commander_tcp_reachable",
                                    value="false",
                                )
                            )
                        else:
                            evidence.append(
                                pb.Evidence(
                                    name="commander_tcp_reachable",
                                    value="true",
                                )
                            )
                            status = "UNKNOWN"
                            limitations.append(
                                "TCP reachability does not prove Commander RPC health"
                            )
                else:
                    status = "NOT_SUPPORTED"
                # Query one known Commander command without creating a new
                # command or retrying a prior submission.
                accepted = next(
                    (
                        record
                        for record in reversed(self._runtime.forwarder.records)
                        if record.state is ForwardState.ACCEPTED
                        and record.command_id
                        and (not target or record.gateway_id == target)
                    ),
                    None,
                )
                if accepted is not None:
                    try:
                        result = await asyncio.wait_for(
                            self._runtime.forwarder.get_command_result(accepted.command_id),
                            timeout=1.0,
                        )
                    except (
                        grpc.RpcError,
                        OSError,
                        RuntimeError,
                        TimeoutError,
                        ValueError,
                    ) as exc:
                        limitations.append(f"Commander result query unavailable: {exc}")
                    else:
                        evidence.append(
                            pb.Evidence(
                                name="last_commander_command_id",
                                value=accepted.command_id,
                            )
                        )
                        evidence.append(
                            pb.Evidence(
                                name="last_commander_command_state",
                                value=result.state,
                            )
                        )
                limitations.append("diagnosis never submits real PLC commands")
            stage = pb.DiagnosticStage(
                stage=name,
                status=status,
                duration_ms=int((perf_counter() - start) * 1000),
                error_code=error_code,
                message=message,
                evidence=evidence,
                source="gateway_runtime",
            )
            stage.observed_at.FromDatetime(datetime.now(UTC))
            observed.append(stage)
        statuses = [stage.status for stage in observed]
        if "FAIL" in statuses:
            overall = "FAIL"
        elif statuses and all(status == "PASS" for status in statuses):
            overall = "PASS"
        else:
            overall = "UNKNOWN"
        return pb.DiagnoseResponse(overall_status=overall, stages=observed, limitations=limitations)
