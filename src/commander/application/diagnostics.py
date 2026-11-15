"""Commander diagnostics orchestration without RPC or socket dependencies."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from commander.application.service_lifecycle import CommanderService
from core.configuration.effective import resolve_effective_device
from core.domain import DeviceId


class TcpProbePort(Protocol):
    async def probe(self, host: str, port: int, timeout: float) -> None: ...


@dataclass(frozen=True, slots=True)
class DiagnosticStep:
    name: str
    status: str
    evidence_name: str
    evidence_value: str
    message: str = ""
    error_code: str = ""
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    status: str
    stages: tuple[DiagnosticStep, ...]
    limitations: tuple[str, ...]


class CommanderDiagnostics:
    """Read-only health workflow; protocol checks cannot acquire control ownership."""

    DEFAULT_STAGES = ("service", "configuration", "persistence", "audit")
    PERMITTED = frozenset(
        ("service", "configuration", "persistence", "audit", "device", "tcp", "protocol")
    )

    def __init__(self, service: CommanderService, tcp: TcpProbePort) -> None:
        self._service = service
        self._tcp = tcp

    async def run(
        self, stages: tuple[str, ...], target_id: str, timeout_ms: int
    ) -> DiagnosticReport:
        selected = stages or self.DEFAULT_STAGES
        if any(stage not in self.PERMITTED for stage in selected):
            raise ValueError("unsupported diagnostic stage")
        deadline = time.perf_counter() + (timeout_ms or 1000) / 1000
        health = self._service.health()
        results: list[DiagnosticStep] = []
        for name in selected:
            began = time.perf_counter()
            status = ""
            message = ""
            code = ""
            evidence: tuple[str, str]
            if began > deadline:
                results.append(
                    DiagnosticStep(name, "TIMEOUT", "", "", error_code="DEADLINE_EXCEEDED")
                )
                continue
            if name == "service":
                healthy = health.ready
                evidence = ("state", health.state.value)
            elif name == "configuration":
                healthy = bool(health.revision)
                evidence = ("revision", health.revision)
            elif name == "persistence":
                healthy = health.persistence_operational and health.journal_operational
                evidence = ("pending", str(health.audit_pending_count))
            elif name == "audit":
                healthy = health.audit_operational
                evidence = ("published", str(health.audit_published))
            else:
                if not target_id:
                    raise ValueError("target_id required")
                snapshot = self._service.config_manager.snapshot
                device = snapshot.site.devices.get(DeviceId(target_id))
                healthy = device is not None and device.enabled
                evidence = ("configured", str(healthy))
                if name == "protocol" and healthy and device is not None:
                    state, capabilities = self._service.executor.inspect_existing_session(target_id)
                    healthy = state == "CONNECTED"
                    if state in {"NOT_CONNECTED", "DISCONNECTED", "CLOSING"}:
                        status = "NOT_RUN"
                        message = (
                            "No settled existing protocol session available; "
                            "diagnostics never initiate control connections"
                        )
                    elif state == "BLOCKED":
                        status = "FAIL"
                        message = "Device has unsettled native I/O"
                    evidence = (
                        "session_capabilities",
                        ",".join(capabilities) if healthy else state,
                    )
                if name == "tcp" and healthy and device is not None:
                    model = snapshot.device_models[device.device_model_id]
                    table = snapshot.point_tables[model.point_table_id]
                    effective, _ = resolve_effective_device(device, model, table.protocol.name)
                    host = effective.endpoint.host
                    port = effective.endpoint.port
                    if port is None:
                        healthy = False
                        evidence = ("error", "resolved endpoint has no TCP port")
                    else:
                        try:
                            await self._tcp.probe(
                                host, port, max(0.0, deadline - time.perf_counter())
                            )
                            evidence = ("tcp_endpoint", f"{host}:{port}")
                        except (OSError, TimeoutError):
                            healthy = False
                            evidence = ("tcp_endpoint", f"{host}:{port}")
            results.append(
                DiagnosticStep(
                    name=name,
                    status=status or ("PASS" if healthy else "FAIL"),
                    evidence_name=evidence[0],
                    evidence_value=evidence[1],
                    message=message,
                    error_code=code,
                    duration_ms=int((time.perf_counter() - began) * 1000),
                )
            )
        states = {item.status for item in results}
        overall = (
            "FAIL"
            if states.intersection({"FAIL", "TIMEOUT"})
            else "PARTIAL"
            if "NOT_RUN" in states
            else "PASS"
        )
        return DiagnosticReport(
            overall,
            tuple(results),
            (
                "TCP stage proves port reachability only, not protocol handshake",
                "Protocol stage reads existing session state only; no new handshake",
                "Dependency stages report aggregate health, not individual transport readiness",
            ),
        )
