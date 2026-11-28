"""Application diagnostic scenario runner; transport probes are injected ports."""

from __future__ import annotations

import asyncio
import errno
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from collector.application.acquisition import CollectorAcquisition
from core.application import ProtocolSample


class PublisherEvidence(Protocol):
    @property
    def connected(self) -> bool: ...
    @property
    def delivery_mode(self) -> str: ...
    def validate_encoding(
        self,
        device_id: str,
        sample: ProtocolSample,
        *,
        device_type: str,
        point_table_id: str,
        task_id: str,
        device_config_id: str,
        sample_time: datetime,
        expected_point_ids: tuple[str, ...],
    ) -> None: ...


class NetworkProbe(Protocol):
    async def probe(self, host: str, port: int, timeout: float) -> None: ...


@dataclass(frozen=True, slots=True)
class DiagnosticStep:
    name: str
    status: str
    message: str
    error_code: str
    evidence: tuple[tuple[str, str], ...]
    duration_ms: int


@dataclass(frozen=True, slots=True)
class DiagnosticReport:
    status: str
    stages: tuple[DiagnosticStep, ...]
    limitations: tuple[str, ...]


class CollectorDiagnostics:
    """One bounded read-only diagnostic workflow without gRPC or TCP primitives."""

    DEFAULT_STAGES = ("NETWORK", "PROTOCOL", "ACQUISITION", "NATS", "BOUNDARY")
    SUPPORTED = frozenset(DEFAULT_STAGES)

    def __init__(
        self,
        acquisition: CollectorAcquisition,
        publisher: PublisherEvidence,
        network: NetworkProbe,
    ) -> None:
        self._acquisition = acquisition
        self._publisher = publisher
        self._network = network

    async def run(
        self, target_id: str, stages: tuple[str, ...], timeout_ms: int
    ) -> DiagnosticReport:
        workers = self._acquisition.workers
        worker = workers.get(target_id) if target_id else None
        if target_id and worker is None:
            raise LookupError("unknown diagnostic target")
        selected = stages or self.DEFAULT_STAGES
        deadline = time.monotonic() + min((timeout_ms or 10000) / 1000, 30.0)
        results: list[DiagnosticStep] = []
        limitations: list[str] = []
        for name in selected:
            began = time.monotonic()
            status = "NOT_RUN"
            detail = "not evaluated"
            code = ""
            evidence: list[tuple[str, str]] = []
            if time.monotonic() >= deadline:
                detail = "diagnostic deadline expired"
            elif name not in self.SUPPORTED:
                detail = "unknown diagnostic stage"
            elif name == "BOUNDARY":
                status, detail = "PASS", "Collector evidence is not Sink persistence"
                limitations.append("NATS and protocol checks do not verify downstream Sink storage")
            elif name == "NATS":
                if self._publisher.connected:
                    status, detail = "PASS", "NATS client transport connected"
                else:
                    status, code, detail = (
                        "UNKNOWN",
                        "NATS_CONNECTION",
                        "NATS transport is not currently connected",
                    )
                evidence.append(("delivery", self._publisher.delivery_mode))
                evidence.extend(
                    ("sample", json.dumps(item, separators=(",", ":")))
                    for item in getattr(self._publisher, "last_sample_evidence", ())
                    if not target_id or item.get("device_id") == target_id
                )
                limitations.append(
                    "NATS stage verifies the client transport only; no isolated "
                    "publish, JetStream PubAck or Sink persistence is verified"
                )
            elif worker is None:
                detail = "no available device target"
            elif name == "NETWORK":
                endpoint = worker.plan.device.endpoint
                port = endpoint.port
                if port is None:
                    detail = "no effective TCP port"
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining == 0:
                        detail = "diagnostic deadline expired"
                    else:
                        try:
                            await self._network.probe(endpoint.host, port, min(remaining, 3.0))
                            status, detail = "PASS", "TCP port reachable"
                        except TimeoutError:
                            if time.monotonic() >= deadline:
                                detail = "diagnostic deadline expired"
                            else:
                                status, code, detail = (
                                    "FAIL",
                                    "TIMEOUT",
                                    "TCP connect timed out",
                                )
                        except OSError as exc:
                            status, detail = "FAIL", str(exc)
                            code = "PORT" if exc.errno == errno.ECONNREFUSED else "NETWORK"
                    evidence.extend((("host", endpoint.host), ("port", str(port))))
            elif name == "PROTOCOL":
                if not worker.driver.is_open():
                    detail = "protocol session is not running"
                else:
                    remaining = max(0.0, deadline - time.monotonic())
                    if remaining == 0:
                        detail = "diagnostic deadline expired"
                    else:
                        try:
                            healthy = await asyncio.wait_for(
                                asyncio.shield(worker.submit_health()),
                                timeout=min(remaining, 3.0),
                            )
                            status = "PASS" if healthy else "FAIL"
                            code = "" if healthy else "PROTOCOL"
                            detail = (
                                "protocol health response"
                                if healthy
                                else worker.last_error or "protocol health failed"
                            )
                        except TimeoutError:
                            if time.monotonic() >= deadline:
                                detail = "diagnostic deadline expired"
                            else:
                                status, code, detail = (
                                    "UNKNOWN",
                                    "TIMEOUT",
                                    "protocol health not settled",
                                )
            elif name == "ACQUISITION":
                samples = worker.last_samples
                if not samples:
                    detail = "no captured sample in current process"
                else:
                    newest = max(sample.timestamp for sample in samples)
                    age = (datetime.now(UTC) - newest).total_seconds()
                    evidence.append(("newest_sample_age_ms", str(max(0, int(age * 1000)))))
                    if age > min(worker.timeout, worker.watchdog_interval):
                        status, code, detail = (
                            "UNKNOWN",
                            "STALE_EVIDENCE",
                            "cached samples are stale; acquisition freshness unverified",
                        )
                    else:
                        good = sum(sample.quality.value == "good" for sample in samples)
                        encoded = 0
                        encoding_failed = False
                        for sample in samples:
                            try:
                                self._publisher.validate_encoding(
                                    target_id,
                                    sample,
                                    device_type=worker.plan.device_type,
                                    point_table_id=worker.plan.point_table_id,
                                    task_id=worker.plan.task_id,
                                    device_config_id=worker.plan.device_config_id,
                                    sample_time=datetime.now(UTC),
                                    expected_point_ids=tuple(worker.plan.point_table.points),
                                )
                            except (ValueError, TypeError) as exc:
                                encoding_failed = True
                                detail = f"sample encoding failed: {exc}"
                            else:
                                encoded += 1
                        if good == len(samples) and encoded == len(samples):
                            status = "PASS"
                        elif good != len(samples):
                            status, code = "FAIL", "POINT"
                        elif encoding_failed:
                            status, code = "FAIL", "NORMALIZATION"
                        else:
                            status = "UNKNOWN"
                        if not detail.startswith("sample encoding failed"):
                            detail = "recent sample quality and transport encoding"
                        evidence.extend(
                            (
                                ("total_points", str(len(samples))),
                                ("good_points", str(good)),
                                ("encoded_points", str(encoded)),
                            )
                        )
            # A probe settling after the caller's budget is not evidence of
            # failure or success within that budget.
            if time.monotonic() >= deadline and status != "NOT_RUN":
                status, detail, code = "NOT_RUN", "diagnostic deadline expired", ""
                evidence.clear()
            if status in {"FAIL", "UNKNOWN", "NOT_RUN"} and not code:
                code = status
            results.append(
                DiagnosticStep(
                    name=name,
                    status=status,
                    message=detail,
                    error_code=code,
                    evidence=tuple(evidence),
                    duration_ms=int((time.monotonic() - began) * 1000),
                )
            )
        statuses = {item.status for item in results}
        overall = (
            "UNKNOWN"
            if not results
            else "FAIL"
            if "FAIL" in statuses
            else "UNKNOWN"
            if "UNKNOWN" in statuses or "NOT_RUN" in statuses
            else "PASS"
        )
        if not target_id:
            limitations.append("No target device specified; device chain stages were not run")
        return DiagnosticReport(overall, tuple(results), tuple(limitations))
