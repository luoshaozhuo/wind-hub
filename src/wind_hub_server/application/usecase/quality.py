"""采集/交付质量聚合。

质量数据来自 Server 本地监控历史和最近一次 Collector 低频运行快照；
不直接访问 Collector Runtime，也不承载采集数据流。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from wind_hub_server.application.port.monitoring import HostSnapshot, MonitoringSnapshotPort
from wind_hub_server.application.usecase.config import ConfigUseCase


def _list_field(payload: dict[str, object], name: str) -> list[object]:
    """读取快照 dict 中的列表字段（缺失或非列表按空列表处理）。"""
    value = payload.get(name)
    return list(value) if isinstance(value, list) else []


def _int_field(payload: dict[str, object], name: str) -> int:
    """读取快照 dict 中的整型字段（缺失或非 int 按 0 处理）。"""
    value = payload.get(name)
    return value if isinstance(value, int) else 0


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
    """按 Collector 运行快照与本地历史计算 1h/24h/7d 质量视图。"""

    def __init__(
        self,
        config: ConfigUseCase,
        monitoring: MonitoringSnapshotPort,
    ) -> None:
        self._config = config
        self._monitoring = monitoring

    async def snapshot(
        self,
        window: QualityWindow,
        *,
        refresh: bool = False,
    ) -> QualitySnapshot:
        """从低频监控快照计算质量视图；显式 check 可要求立即刷新。"""
        if refresh:
            await self._monitoring.refresh_now()

        seconds = {"1h": 3600, "24h": 86400, "7d": 604800}[window]
        now = datetime.now(UTC)
        since = now - timedelta(seconds=seconds)
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
            for event in self._monitoring.events_since(since)
        ]
        issues = self._issues()
        stale = sum(
            1
            for issue in issues
            if issue.dimension == "Continuity" and issue.kind == "Task"
        )
        worker_faults = sum(1 for issue in issues if issue.kind == "Worker")
        continuity_faults = stale + worker_faults
        dropped = delta["points_dropped"]
        bad = delta["points_bad"]
        missed = delta["missed_cycles"]
        failures = delta["acquisition_failures"]
        total_points = max(0, delta["points_total"])
        runs = max(0, delta["acquisition_runs"])
        expected_cycles = runs + missed
        completeness = (
            100.0
            if expected_cycles == 0
            else runs / expected_cycles * 100.0
        )

        dimensions = [
            QualityDimension(
                key="continuity",
                dimension="Continuity",
                status="Fault" if continuity_faults else "Normal",
                metric=f"{stale} stale task(s), {worker_faults} worker fault(s)",
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
        interrupted = sum(
            1
            for row in acquisition + delivery
            if row.state == "Interrupted"
        )
        degraded = sum(
            1
            for row in acquisition + delivery
            if row.state == "Degraded"
        )
        return QualitySnapshot(
            window=window,
            sampled_from=first.timestamp,
            sampled_to=last.timestamp,
            acquisition_channels=acquisition,
            delivery_channels=delivery,
            channel_summary=[
                self._metric(
                    "interrupted",
                    "Interrupted",
                    interrupted,
                    "current",
                    "Fault",
                ),
                self._metric(
                    "degraded",
                    "Degraded",
                    degraded,
                    "current",
                    "Warning",
                ),
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
                self._metric(
                    "stale",
                    "Stale Tasks",
                    stale,
                    "current",
                    "Fault",
                ),
                self._metric(
                    "missing",
                    "Missing Cycles",
                    missed,
                    window,
                    "Warning",
                ),
                self._metric(
                    "reads",
                    "Point Read Failures",
                    bad,
                    window,
                    "Warning",
                ),
                self._metric(
                    "dropped",
                    "Dropped Points",
                    dropped,
                    window,
                    "Fault",
                ),
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
        devices = self._monitoring.devices_snapshot()
        status = self._monitoring.runtime_status()
        acquisitions = _list_field(status, "acquisitions")
        rows: list[QualityChannel] = []

        for device in sorted(
            devices,
            key=lambda item: str(item.get("device_id") or ""),
        ):
            device_id = str(device.get("device_id") or "")
            relevant = [
                item
                for item in acquisitions
                if isinstance(item, dict)
                and str(item.get("device_id") or "") == device_id
            ]
            durations = [
                float(item["last_duration"])
                for item in relevant
                if isinstance(item, dict)
                and item.get("last_duration") is not None
            ]
            failures, reconnects = self._monitoring.device_counts(device_id)
            connected = bool(device.get("connected"))
            rows.append(
                QualityChannel(
                    object=device_id,
                    source="Acquisition",
                    protocol=str(device.get("protocol") or "").upper(),
                    state="Healthy" if connected else "Interrupted",
                    target="configured device",
                    latency_ms=max(durations) * 1000 if durations else None,
                    timeouts=failures,
                    reconnects=reconnects,
                    issue=(
                        str(device.get("last_error"))
                        if device.get("last_error")
                        else None
                    ),
                )
            )
        return rows

    def _delivery_channels(self) -> list[QualityChannel]:
        runtime = {
            str(item.get("name")): item
            for item in self._monitoring.sinks_snapshot()
            if item.get("name") is not None
        }
        rows: list[QualityChannel] = []
        for cfg in self._config.current_config.system.sinks:
            current = runtime.get(cfg.name)
            if not cfg.enabled:
                state = "Disabled"
            elif current is not None and bool(current.get("healthy")):
                state = "Healthy"
            else:
                state = "Interrupted"

            target = str(
                cfg.params.get("bootstrap_servers")
                or cfg.params.get("dsn")
                or cfg.params.get("path")
                or "configured"
            )
            queue_depth = _int_field(current, "queue_depth") if current is not None else 0
            issue = (
                str(current.get("message"))
                if current is not None and current.get("message")
                else None
            )
            if queue_depth > 0 and state == "Healthy":
                state = "Degraded"
                issue = f"queue depth {queue_depth}"
            rows.append(
                QualityChannel(
                    object=cfg.name,
                    source="Delivery",
                    protocol=cfg.type.upper(),
                    state=state,
                    target=target,
                    issue=issue,
                )
            )
        return rows

    def _issues(self) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        status = self._monitoring.runtime_status()
        acquisitions = _list_field(status, "acquisitions")

        for worker_id in _list_field(status, "unavailable_workers"):
            issues.append(
                QualityIssue(
                    level="Fault",
                    object=str(worker_id),
                    kind="Worker",
                    dimension="Continuity",
                    issue="Collector unavailable or identity invalid",
                )
            )

        for item in acquisitions:
            if not isinstance(item, dict):
                continue
            failures = int(item.get("consecutive_failures") or 0)
            if failures <= 0:
                continue
            issues.append(
                QualityIssue(
                    level="Fault",
                    object=str(item.get("task_id") or item.get("instance_id") or ""),
                    kind="Task",
                    dimension="Continuity",
                    issue="Recent collection failures",
                    error=(
                        str(item.get("last_error"))
                        if item.get("last_error")
                        else None
                    ),
                )
            )

        for device in self._monitoring.devices_snapshot():
            if bool(device.get("connected")):
                continue
            issues.append(
                QualityIssue(
                    level="Fault",
                    object=str(device.get("device_id") or ""),
                    kind="Device",
                    dimension="Continuity",
                    issue="Channel unhealthy",
                    error=(
                        str(device.get("last_error"))
                        if device.get("last_error")
                        else None
                    ),
                )
            )

        for sink in self._monitoring.sinks_snapshot():
            if bool(sink.get("healthy")):
                continue
            issues.append(
                QualityIssue(
                    level="Fault",
                    object=str(sink.get("name") or ""),
                    kind="Sink",
                    dimension="Delivery Integrity",
                    issue="Channel unhealthy",
                    error=(
                        str(sink.get("message"))
                        if sink.get("message")
                        else None
                    ),
                )
            )
        return issues

    @staticmethod
    def _delta(first: HostSnapshot, last: HostSnapshot) -> dict[str, int]:
        return {
            "points_total": max(
                0,
                last.counters.points_total - first.counters.points_total,
            ),
            "points_bad": max(
                0,
                last.counters.points_bad - first.counters.points_bad,
            ),
            "acquisition_runs": max(
                0,
                last.counters.acquisition_runs
                - first.counters.acquisition_runs,
            ),
            "acquisition_failures": max(
                0,
                last.counters.acquisition_failures
                - first.counters.acquisition_failures,
            ),
            "missed_cycles": max(
                0,
                last.counters.missed_cycles - first.counters.missed_cycles,
            ),
            "poll_overruns": max(
                0,
                last.counters.poll_overruns - first.counters.poll_overruns,
            ),
            "connect_failures": max(
                0,
                last.counters.connect_failures - first.counters.connect_failures,
            ),
            "reconnects": max(
                0,
                last.counters.reconnects - first.counters.reconnects,
            ),
            "points_dropped": max(
                0,
                last.points_dropped - first.points_dropped,
            ),
        }
