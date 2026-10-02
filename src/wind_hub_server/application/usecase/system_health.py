"""宿主机与 wind-hub 进程资源健康查询。"""

from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from wind_hub_server.application.port.monitoring import HostSnapshot, MonitoringHistoryPort

HealthRange = Literal["1h", "24h", "7d", "30d"]


class HealthRisk(BaseModel):
    name: str
    state: str
    summary: str
    detail: str


class StorageMount(BaseModel):
    mount: str
    used_gb: float
    total_gb: float
    free_gb: float
    usage_pct: float
    growth_24h_gb: float | None = None
    estimated_full_days: float | None = None


class ResourceSeries(BaseModel):
    timestamps: list[datetime]
    memory_host_gb: list[float | None]
    memory_rss_gb: list[float | None]
    cpu_host_pct: list[float | None]
    cpu_process_pct: list[float | None]
    cpu_temp_c: list[float | None]
    disk_free_gb: list[float | None]


class SystemHealthSnapshot(BaseModel):
    range: HealthRange
    sampled_at: datetime
    uptime_seconds: float
    cpu_count: int
    load_average: tuple[float, float, float] | None
    risks: list[HealthRisk]
    mounts: list[StorageMount]
    series: ResourceSeries
    current: dict[str, float | int | str | None]


class SystemHealthUseCase:
    """从 MonitoringService 生成资源详情、风险和趋势。"""

    def __init__(self, monitoring: MonitoringHistoryPort) -> None:
        self._monitoring = monitoring

    def snapshot(self, range_name: HealthRange) -> SystemHealthSnapshot:
        seconds = {"1h": 3600, "24h": 86400, "7d": 604800, "30d": 2592000}[range_name]
        now = datetime.now(UTC)
        rows = self._monitoring.history(now - timedelta(seconds=seconds))
        current = self._monitoring.capture_now()
        if rows[-1].timestamp != current.timestamp:
            rows.append(current)
        mounts = self._mounts(rows)
        risks = self._risks(current, mounts)
        try:
            load = os.getloadavg()
        except OSError:
            load = None
        return SystemHealthSnapshot(
            range=range_name,
            sampled_at=current.timestamp,
            uptime_seconds=max(
                0.0, (current.timestamp - self._monitoring.started_at).total_seconds()
            ),
            cpu_count=max(1, os.cpu_count() or 1),
            load_average=load,
            risks=risks,
            mounts=mounts,
            series=ResourceSeries(
                timestamps=[row.timestamp for row in rows],
                memory_host_gb=[row.memory_used_gb for row in rows],
                memory_rss_gb=[row.process_rss_gb for row in rows],
                cpu_host_pct=[row.cpu_host_pct for row in rows],
                cpu_process_pct=[row.cpu_process_pct for row in rows],
                cpu_temp_c=[row.cpu_temp_c for row in rows],
                disk_free_gb=[row.disk_free_gb for row in rows],
            ),
            current={
                "memory_used_gb": current.memory_used_gb,
                "memory_total_gb": current.memory_total_gb,
                "process_rss_gb": current.process_rss_gb,
                "cpu_host_pct": current.cpu_host_pct,
                "cpu_process_pct": current.cpu_process_pct,
                "cpu_temp_c": current.cpu_temp_c,
                "disk_free_gb": current.disk_free_gb,
                "disk_total_gb": current.disk_total_gb,
            },
        )

    @staticmethod
    def _risks(current: HostSnapshot, mounts: list[StorageMount]) -> list[HealthRisk]:
        risks: list[HealthRisk] = []
        memory_pct = (
            current.memory_used_gb / current.memory_total_gb * 100
            if current.memory_used_gb is not None
            and current.memory_total_gb not in (None, 0)
            else None
        )
        cpu = current.cpu_host_pct
        temp = current.cpu_temp_c
        disk_pct = max((mount.usage_pct for mount in mounts), default=0.0)
        checks = [
            ("CPU", cpu, 85.0, "%"),
            ("Memory", memory_pct, 85.0, "%"),
            ("Storage", disk_pct, 85.0, "%"),
            ("Thermal", temp, 85.0, "°C"),
        ]
        for name, value, warning, unit in checks:
            if value is None:
                risks.append(
                    HealthRisk(
                        name=name, state="Unknown", summary="No data", detail="Metric unavailable"
                    )
                )
                continue
            state = "Fault" if value >= 95 else "Warning" if value >= warning else "Healthy"
            risks.append(
                HealthRisk(
                    name=name,
                    state=state,
                    summary=f"{value:.1f}{unit}",
                    detail=f"warning threshold {warning:.0f}{unit}",
                )
            )
        return risks

    @staticmethod
    def _mounts(rows: list[HostSnapshot]) -> list[StorageMount]:
        mounts: list[StorageMount] = []
        candidates: list[str] = ["/"]
        try:
            lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
            allowed = {"ext4", "xfs", "btrfs", "vfat", "zfs"}
            candidates.extend(
                parts[1]
                for line in lines
                if len(parts := line.split()) >= 3 and parts[2] in allowed
            )
        except OSError:
            pass
        first_free = rows[0].disk_free_gb if rows else None
        last_free = rows[-1].disk_free_gb if rows else None
        hours = (
            (rows[-1].timestamp - rows[0].timestamp).total_seconds() / 3600
            if len(rows) > 1
            else 0
        )
        root_growth_24h = None
        if first_free is not None and last_free is not None and hours > 0:
            root_growth_24h = max(0.0, (first_free - last_free) * 24 / hours)
        for mount in dict.fromkeys(candidates):
            try:
                usage = shutil.disk_usage(mount)
            except OSError:
                continue
            used = usage.used / 1024**3
            total = usage.total / 1024**3
            free = usage.free / 1024**3
            growth = root_growth_24h if mount == "/" else None
            estimated = free / growth if growth is not None and growth > 0 else None
            mounts.append(
                StorageMount(
                    mount=mount,
                    used_gb=used,
                    total_gb=total,
                    free_gb=free,
                    usage_pct=used / total * 100 if total else 0.0,
                    growth_24h_gb=growth,
                    estimated_full_days=estimated,
                )
            )
        return mounts
