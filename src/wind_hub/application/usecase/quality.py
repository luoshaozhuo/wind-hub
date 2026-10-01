"""真实采集/交付质量聚合。"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from wind_hub.application.port.monitoring import (
    HostSnapshot,
    MonitoringHistoryPort,
    MonitoringMetricsQueryPort,
)
from wind_hub.application.runtime.runtime import Runtime
from wind_hub.application.usecase.config import ConfigUseCase

QualityWindow = Literal["1h", "24h", "7d"]


class QualityChannel(BaseModel):
    object: str
    source: str
    protocol: str
    state: str
    target: str
    latency_ms: float | None = None
    timeouts: int = 0
    reconnects: int = 0
    issue: str | None = None


class QualityMetric(BaseModel):
    key: str
    label: str
    value: float | int
    hint: str
    status: str


class QualityDimension(BaseModel):
    key: str
    dimension: str
    status: str
    metric: str
    detail: str


class QualityIssue(BaseModel):
    level: str
    object: str
    kind: str
    dimension: str
    issue: str
    duration_seconds: float | None = None
    error: str | None = None


class CommunicationEvent(BaseModel):
    timestamp: datetime
    object: str
    event: str
    state: str
    evidence: str


class QualitySnapshot(BaseModel):
    window: QualityWindow
    sampled_from: datetime
    sampled_to: datetime
    acquisition_channels: list[QualityChannel]
    delivery_channels: list[QualityChannel]
    channel_summary: list[QualityMetric]
    data_metrics: list[QualityMetric]
    dimensions: list[QualityDimension]
    issues: list[QualityIssue]
    events: list[CommunicationEvent] = Field(default_factory=list)


class QualityUseCase:
    """按真实监控样本计算 1h/24h/7d 质量快照。"""

    def __init__(
        self,
        runtime: Runtime,
        config: ConfigUseCase,
        metrics: MonitoringMetricsQueryPort,
        monitoring: MonitoringHistoryPort,
    ) -> None:
        self._runtime = runtime
        self._config = config
        self._metrics = metrics
        self._monitoring = monitoring

    def snapshot(self, window: QualityWindow) -> QualitySnapshot:
        seconds = {"1h": 3600, "24h": 86400, "7d": 604800}[window]
        now = datetime.now(UTC)
        since = now - timedelta(seconds=seconds)
        self._monitoring.capture_now()
        samples = self._monitoring.history(since)
        first, last = samples[0], samples[-1]
        delta = self._delta(first, last)
        acquisition = self._acquisition_channels()
        delivery = self._delivery_channels()
        events = [
            CommunicationEvent(
                timestamp=event.timestamp,
                object=event.object,
                event=event.kind,
                state="Active" if event.kind.endswith("failed") else "Recovered",
                evidence=event.message,
            )
            for event in self._metrics.events_since(since)
        ]
        issues = self._issues()
        stale = sum(1 for issue in issues if issue.dimension == "Continuity")
        dropped = delta["points_dropped"]
        bad = delta["points_bad"]
        missed = delta["missed_cycles"]
        failures = delta["acquisition_failures"]
        total_points = max(0, delta["points_total"])
        runs = max(0, delta["acquisition_runs"])
        expected_cycles = runs + missed
        completeness = (
            100.0 if expected_cycles == 0 else runs / expected_cycles * 100.0
        )
        dimensions = [
            QualityDimension(
                key="continuity",
                dimension="Continuity",
                status="Fault" if stale else "Normal",
                metric=f"{stale} stale task(s)",
                detail=f"{failures} failed collect(s) in window",
            ),
            QualityDimension(
                key="timeliness",
                dimension="Timeliness",
                status="Warning" if delta["poll_overruns"] else "Normal",
                metric=f"{delta['poll_overruns']} overrun(s)",
                detail=f"{missed} missed cycle(s)",
            ),
            QualityDimension(
                key="completeness",
                dimension="Completeness",
                status=(
                    "Fault"
                    if completeness < 99
                    else "Warning"
                    if completeness < 99.9
                    else "Normal"
                ),
                metric=f"{missed} missing cycle(s)",
                detail=f"{completeness:.3f}% scheduled cycles completed",
            ),
            QualityDimension(
                key="validity",
                dimension="Validity",
                status="Warning" if bad else "Normal",
                metric=f"{bad} BAD point(s)",
                detail=f"{total_points} point value(s) observed",
            ),
            QualityDimension(
                key="delivery",
                dimension="Delivery Integrity",
                status="Fault" if dropped else "Normal",
                metric=f"{dropped} dropped point(s)",
                detail=f"{last.points_routed - first.points_routed} routed point(s)",
            ),
        ]
        interrupted = sum(1 for row in acquisition + delivery if row.state == "Interrupted")
        degraded = sum(1 for row in acquisition + delivery if row.state == "Degraded")
        return QualitySnapshot(
            window=window,
            sampled_from=first.timestamp,
            sampled_to=last.timestamp,
            acquisition_channels=acquisition,
            delivery_channels=delivery,
            channel_summary=[
                self._metric(
                    "interrupted", "Interrupted", interrupted, "current", "Fault"
                ),
                self._metric("degraded", "Degraded", degraded, "current", "Warning"),
                self._metric(
                    "timeouts",
                    "Timeouts",
                    delta["connect_failures"],
                    window,
                    "Warning",
                ),
                self._metric(
                    "reconnects",
                    "Reconnects",
                    delta["reconnects"],
                    window,
                    "Warning",
                ),
            ],
            data_metrics=[
                self._metric("stale", "Stale Tasks", stale, "current", "Fault"),
                self._metric("missing", "Missing Cycles", missed, window, "Warning"),
                self._metric("reads", "Point Read Failures", bad, window, "Warning"),
                self._metric("dropped", "Dropped Points", dropped, window, "Fault"),
            ],
            dimensions=dimensions,
            issues=issues,
            events=events,
        )

    @staticmethod
    def _metric(
        key: str,
        label: str,
        value: float | int,
        hint: str,
        degraded_status: str,
    ) -> QualityMetric:
        return QualityMetric(
            key=key,
            label=label,
            value=value,
            hint=hint,
            status=degraded_status if value else "Normal",
        )

    def _acquisition_channels(self) -> list[QualityChannel]:
        rows: list[QualityChannel] = []
        states = self._runtime.acquisition_states()
        for device_id, device in sorted(self._runtime.devices.items()):
            health = device.health()
            state = self._runtime.device_state(device_id)
            relevant = [s for s in states.values() if s.device_id == device_id]
            latency = max(
                (s.last_duration for s in relevant if s.last_duration is not None),
                default=None,
            )
            failures, reconnects = self._metrics.device_counts(device_id)
            rows.append(
                QualityChannel(
                    object=device_id,
                    source="Acquisition",
                    protocol=device.config.protocol.upper(),
                    state="Healthy" if health.healthy else "Interrupted",
                    target=f"{device.config.endpoint.host}:{device.config.endpoint.port}",
                    latency_ms=latency * 1000 if latency is not None else None,
                    timeouts=failures,
                    reconnects=reconnects,
                    issue=state.last_error if state is not None else health.message,
                )
            )
        return rows

    def _delivery_channels(self) -> list[QualityChannel]:
        health = self._runtime.health()
        depths = self._runtime.sink_queue_depths()
        rows: list[QualityChannel] = []
        for cfg in self._config.current_config.system.sinks:
            current = health.get(cfg.name)
            if not cfg.enabled:
                state = "Disabled"
            elif current is not None and current.healthy:
                state = "Healthy"
            else:
                state = "Interrupted"
            target = str(
                cfg.params.get("bootstrap_servers")
                or cfg.params.get("dsn")
                or cfg.params.get("path")
                or "configured"
            )
            rows.append(
                QualityChannel(
                    object=cfg.name,
                    source="Delivery",
                    protocol=cfg.type.upper(),
                    state=state,
                    target=target,
                    issue=current.message if current is not None else None,
                    timeouts=0,
                    reconnects=0,
                    latency_ms=None,
                )
            )
            if depths.get(cfg.name, 0) > 0 and rows[-1].state == "Healthy":
                rows[-1].state = "Degraded"
                rows[-1].issue = f"queue depth {depths[cfg.name]}"
        return rows

    def _issues(self) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        now_mono = time.monotonic()
        definitions = self._runtime.task_definitions()
        for instance_id, state in self._runtime.acquisition_states().items():
            definition = definitions.get(state.task_id)
            interval = definition.interval if definition is not None else None
            age = (
                now_mono - state.last_success_at
                if state.last_success_at is not None
                else None
            )
            stale = state.consecutive_failures > 0 or (
                interval is not None and age is not None and age > interval * 3
            )
            if stale:
                issues.append(
                    QualityIssue(
                        level="Fault",
                        object=state.task_id,
                        kind="Task",
                        dimension="Continuity",
                        issue="No fresh samples",
                        duration_seconds=age,
                        error=state.last_error,
                    )
                )
        health = self._runtime.health()
        for name, status in health.items():
            if status.healthy:
                continue
            kind = "Device" if name in self._runtime.devices else "Sink"
            issues.append(
                QualityIssue(
                    level="Fault",
                    object=name,
                    kind=kind,
                    dimension="Continuity" if kind == "Device" else "Delivery Integrity",
                    issue="Channel unhealthy",
                    error=status.message,
                )
            )
        return issues

    @staticmethod
    def _delta(first: HostSnapshot, last: HostSnapshot) -> dict[str, int]:
        return {
            "points_total": max(
                0, last.counters.points_total - first.counters.points_total
            ),
            "points_bad": max(
                0, last.counters.points_bad - first.counters.points_bad
            ),
            "acquisition_runs": max(
                0, last.counters.acquisition_runs - first.counters.acquisition_runs
            ),
            "acquisition_failures": max(
                0,
                last.counters.acquisition_failures
                - first.counters.acquisition_failures,
            ),
            "missed_cycles": max(
                0, last.counters.missed_cycles - first.counters.missed_cycles
            ),
            "poll_overruns": max(
                0, last.counters.poll_overruns - first.counters.poll_overruns
            ),
            "connect_failures": max(
                0,
                last.counters.connect_failures - first.counters.connect_failures,
            ),
            "reconnects": max(
                0, last.counters.reconnects - first.counters.reconnects
            ),
            "points_dropped": max(0, last.points_dropped - first.points_dropped),
        }
