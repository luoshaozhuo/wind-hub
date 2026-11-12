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
                    len(item.config.settings.control.mappings)
                    for item in targets.values()
                    if item.config.settings.control.enabled
                )
                pending = sum(
                    forwarder.pending
                    for gateway_id, forwarder in self._runtime.forwarders.items()
                    if not target or gateway_id == target
                )
                evidence.append(pb.Evidence(name="configured_mappings", value=str(mapped)))
                evidence.append(pb.Evidence(name="forward_events_pending", value=str(pending)))
                if mapped:
                    failures: list[str] = []
                    for gateway_id, item in targets.items():
                        control = item.config.settings.control
                        if not control.enabled:
                            continue
                        endpoint = control.target.endpoint
                        if endpoint is None:
                            failures.append(gateway_id)
                            continue
                        host, _, port = endpoint.rpartition(":")
                        try:
                            connector = asyncio.open_connection(host.strip("[]"), int(port))
                            _, writer = await asyncio.wait_for(connector, timeout=0.5)
                            writer.close()
                            await writer.wait_closed()
                        except (OSError, ValueError, TimeoutError) as exc:
                            failures.append(gateway_id)
                            limitations.append(f"{gateway_id} target TCP unavailable: {exc}")
                        else:
                            evidence.append(
                                pb.Evidence(
                                    name=f"{gateway_id}.target_tcp_reachable",
                                    value="true",
                                )
                            )
                    if failures:
                        status = "FAIL"
                        error_code = "CONTROL_TARGET_UNREACHABLE"
                        message = ",".join(failures)
                    else:
                        status = "UNKNOWN"
                        limitations.append(
                            "TCP reachability does not prove authenticated target RPC health"
                        )
                else:
                    status = "NOT_SUPPORTED"
                # Never issue a new control command merely to diagnose the target.
                for gateway_id, forwarder in self._runtime.forwarders.items():
                    if target and gateway_id != target:
                        continue
                    accepted = next(
                        (
                            record
                            for record in reversed(forwarder.records)
                            if record.state is ForwardState.ACCEPTED and record.command_id
                        ),
                        None,
                    )
                    if accepted is None:
                        continue
                    try:
                        result = await asyncio.wait_for(
                            forwarder.get_command_result(accepted.command_id),
                            timeout=1.0,
                        )
                    except (grpc.RpcError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
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
