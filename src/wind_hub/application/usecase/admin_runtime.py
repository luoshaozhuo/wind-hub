"""Admin Runtime 运维 UseCase：验证、Sink、Diagnostics、Quality、Health。"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import resource
import shutil
import time
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from wind_hub.application.event_log import EventLogStore
from wind_hub.application.operation import OperationManager
from wind_hub.application.runtime.runtime import Runtime
from wind_hub.application.usecase.device_control import DeviceControlUseCase
from wind_hub.application.usecase.query import QueryUseCase
from wind_hub.config.schema import Config
from wind_hub.domain.model.point import PointValue, Quality


class QualityRecorder:
    """记录最近进入采集引擎的点值质量事件。"""

    def __init__(self, max_events: int = 200000) -> None:
        self._events: deque[tuple[datetime, str, str, Quality]] = deque(
            maxlen=max_events
        )

    def observe(self, values: list[PointValue]) -> None:
        for value in values:
            self._events.append(
                (
                    value.timestamp,
                    value.device_id,
                    value.point_id,
                    value.quality,
                )
            )

    def window(
        self,
        seconds: int,
    ) -> list[tuple[datetime, str, str, Quality]]:
        cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
        return [event for event in self._events if event[0] >= cutoff]


class DeviceVerifyUseCase:
    """设备网络/协议/点位验证；只读，不改变配置。"""

    def __init__(
        self,
        runtime: Runtime,
        query: QueryUseCase,
        logs: EventLogStore,
    ) -> None:
        self._runtime = runtime
        self._query = query
        self._logs = logs

    async def verify(self, device_id: str) -> dict[str, Any]:
        device = self._runtime.devices.get(device_id)
        if device is None:
            raise KeyError(device_id)

        started = time.monotonic()
        errors: list[dict[str, str]] = []
        health = device.health()
        network = "success" if health.healthy else "failed"
        protocol = "success" if health.healthy else "failed"
        points = "unknown"
        total = len(device.points)
        ok = 0
        failed = 0

        if not health.healthy:
            errors.append(
                {
                    "stage": "network",
                    "target": device.config.endpoint.host,
                    "message": health.message or "device unavailable",
                }
            )
        elif device.points:
            points = "success"
            for point in device.points[: min(8, total)]:
                try:
                    await self._query.read_point(device_id, point.point_id)
                    ok += 1
                except Exception as exc:
                    failed += 1
                    points = "partial" if ok else "failed"
                    errors.append(
                        {
                            "stage": "points",
                            "target": point.point_id,
                            "message": str(exc),
                        }
                    )

        state = (
            "success"
            if not errors
            else ("warning" if points == "partial" else "failed")
        )
        result = {
            "state": state,
            "network": network,
            "protocol": protocol,
            "points": points,
            "point_total": total,
            "point_success": ok,
            "point_failed": failed,
            "verified_at": datetime.now(UTC).isoformat(),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "errors": errors,
        }
        self._logs.append(
            "INFO" if state != "failed" else "ERROR",
            "device",
            device_id,
            f"verification {state}",
        )
        return result

    async def verify_all(
        self,
        device_ids: list[str],
    ) -> dict[str, Any]:
        results = [
            await self.verify(device_id)
            for device_id in device_ids
        ]
        return {
            "total": len(results),
            "failed": sum(
                result["state"] == "failed" for result in results
            ),
            "warning": sum(
                result["state"] == "warning" for result in results
            ),
            "results": results,
        }


class SinkUseCase:
    """真实 Sink 状态、验证和合成写入测试。"""

    def __init__(
        self,
        runtime: Runtime,
        logs: EventLogStore,
    ) -> None:
        self._runtime = runtime
        self._logs = logs

    def list(self) -> list[dict[str, Any]]:
        depths = self._runtime.sink_queue_depths()
        return [
            {
                "name": name,
                "healthy": sink.health().healthy,
                "message": sink.health().message,
                "queue_depth": depths.get(name, 0),
            }
            for name, sink in self._runtime.sinks.items()
        ]

    async def verify(self, name: str) -> dict[str, Any]:
        sink = self._runtime.sinks.get(name)
        if sink is None:
            raise KeyError(name)

        started = time.monotonic()
        health = sink.health()
        state = "passed" if health.healthy else "failed"
        result = {
            "state": state,
            "checked_at": datetime.now(UTC).isoformat(),
            "passed": 1 if health.healthy else 0,
            "total": 1,
            "checks": [
                {
                    "layer": "runtime",
                    "name": "Sink health",
                    "state": state,
                    "target": name,
                    "latency_ms": round(
                        (time.monotonic() - started) * 1000,
                        1,
                    ),
                    "detail": health.message
                    or ("healthy" if health.healthy else "unhealthy"),
                    "error_code": ""
                    if health.healthy
                    else "SINK_UNHEALTHY",
                }
            ],
        }
        self._logs.append(
            "INFO" if health.healthy else "ERROR",
            "sink",
            name,
            f"verification {state}",
        )
        return result

    async def write_test(self, name: str) -> dict[str, Any]:
        sink = self._runtime.sinks.get(name)
        if sink is None:
            raise KeyError(name)

        started = time.monotonic()
        value = PointValue(
            device_id="__wind_hub_test__",
            point_id="write_test",
            value=1.0,
            source="admin",
        )
        try:
            await sink.write([value])
            await sink.flush()
        except Exception as exc:
            self._logs.append(
                "ERROR",
                "sink",
                name,
                f"write test failed: {exc}",
            )
            return {
                "ok": False,
                "title": "Write test failed",
                "detail": str(exc),
                "latency": round(
                    (time.monotonic() - started) * 1000,
                    1,
                ),
            }

        latency = round((time.monotonic() - started) * 1000, 1)
        self._logs.append(
            "INFO",
            "sink",
            name,
            "write test passed",
        )
        return {
            "ok": True,
            "title": "Write test passed",
            "detail": "Synthetic PointValue accepted by sink.",
            "latency": latency,
        }


class DiagnosticUseCase:
    """网络与协议诊断。"""

    def __init__(
        self,
        query: QueryUseCase,
        control: DeviceControlUseCase,
        operations: OperationManager,
        logs: EventLogStore,
    ) -> None:
        self._query = query
        self._control = control
        self._operations = operations
        self._logs = logs

    async def ping(self, host: str) -> dict[str, str]:
        proc = await asyncio.create_subprocess_exec(
            "ping",
            "-c",
            "1",
            "-W",
            "1",
            host,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        text = out.decode(errors="ignore")
        reachable = proc.returncode == 0
        rtt = "—"
        if "time=" in text:
            rtt = text.split("time=", 1)[1].split()[0] + " ms"
        return {
            "IP": host,
            "Reachable": "Yes" if reachable else "No",
            "RTT": rtt,
            "Loss": "0%" if reachable else "100%",
        }

    async def tcp(
        self,
        host: str,
        ports: list[int],
    ) -> list[dict[str, str | int]]:
        rows: list[dict[str, str | int]] = []
        for port in ports:
            started = time.monotonic()
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=1.0,
                )
                writer.close()
                await writer.wait_closed()
                state = "Open"
            except Exception:
                state = "Closed"
            rows.append(
                {
                    "IP": host,
                    "Port": port,
                    "State": state,
                    "Latency": (
                        f"{(time.monotonic() - started) * 1000:.0f} ms"
                        if state == "Open"
                        else "—"
                    ),
                }
            )
        return rows

    async def protocol_read(
        self,
        device_id: str,
        point_id: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        value = await self._query.read_point(device_id, point_id)
        return {
            "Target": device_id,
            "Point": point_id,
            "Value": value.value,
            "Quality": value.quality.value,
            "Timestamp": value.timestamp.isoformat(),
            "Result": "Success",
            "Latency": (
                f"{(time.monotonic() - started) * 1000:.0f} ms"
            ),
        }

    async def protocol_write(
        self,
        device_id: str,
        point_id: str,
        value: Any,
    ) -> dict[str, Any]:
        result = await self._control.send(
            device_id,
            point_id,
            value,
        )
        return {
            "Target": device_id,
            "Point": point_id,
            "Write": value,
            "Result": "Success" if result.success else "Failed",
            "Readback": result.readback,
            "Error": (
                result.error
                or result.readback_error
                or "—"
            ),
            "Latency": f"{result.latency_ms:.0f} ms",
        }

    async def start_scan(
        self,
        cidr: str,
        ports: list[int],
    ) -> str:
        network = ipaddress.ip_network(cidr, strict=False)
        hosts = list(network.hosts())
        if len(hosts) > 4096:
            raise ValueError(
                "subnet scan is limited to 4096 hosts"
            )
        op = self._operations.create(
            "diagnostics.subnet_scan",
            total=len(hosts),
        )
        asyncio.create_task(
            self._run_scan(
                op.operation_id,
                [str(host) for host in hosts],
                ports,
            )
        )
        return op.operation_id

    async def _run_scan(
        self,
        operation_id: str,
        hosts: list[str],
        ports: list[int],
    ) -> None:
        self._operations.mark_running(operation_id)
        rows: list[dict[str, Any]] = []
        try:
            for index, host in enumerate(hosts, 1):
                probe = (
                    await self.tcp(host, ports[:3])
                    if ports
                    else []
                )
                open_ports = [
                    row["Port"]
                    for row in probe
                    if row["State"] == "Open"
                ]
                if open_ports:
                    rows.append(
                        {
                            "IP": host,
                            "Ports": open_ports,
                        }
                    )
                self._operations.update_progress(
                    operation_id,
                    completed=index,
                )
            self._operations.succeed(
                operation_id,
                {"hosts": rows},
            )
        except Exception as exc:
            self._operations.fail(
                operation_id,
                code="DIAGNOSTIC_SCAN_FAILED",
                message=str(exc),
            )


class QualityUseCase:
    """从真实采集事件和 Runtime 状态派生窗口质量快照。"""

    WINDOWS = {
        "1 h": 3600,
        "24 h": 86400,
        "7 d": 604800,
    }

    def __init__(
        self,
        runtime: Runtime,
        config_getter: Callable[[], Config],
        recorder: QualityRecorder,
    ) -> None:
        self._runtime = runtime
        self._config_getter = config_getter
        self._recorder = recorder

    def snapshot(self, window: str) -> dict[str, Any]:
        seconds = self.WINDOWS.get(window, 86400)
        events = self._recorder.window(seconds)
        bad = sum(
            1
            for event in events
            if event[3] is Quality.BAD
        )
        received = len(events)
        cfg = self._config_getter()
        expected = self._expected_samples(cfg, seconds)
        missing = max(0, expected - received)
        completeness = (
            100.0
            if expected == 0
            else max(
                0.0,
                min(100.0, received / expected * 100),
            )
        )

        acquisition = list(
            self._runtime.acquisition_states().values()
        )
        failed = [
            state
            for state in acquisition
            if state.consecutive_failures > 0
        ]
        timeout_count = sum(
            1
            for state in failed
            if "timeout" in (state.last_error or "").lower()
        )
        offline = [
            device_id
            for device_id, device in self._runtime.devices.items()
            if not device.health().healthy
        ]
        reconnects = sum(
            (
                self._runtime.device_state(device_id).consecutive_failures
                if self._runtime.device_state(device_id) is not None
                else 0
            )
            for device_id in self._runtime.devices
        )

        dimensions = [
            {
                "key": "continuity",
                "dimension": "Continuity",
                "status": "Fault" if failed else "Normal",
                "metric": f"{len(failed)} stale tasks",
                "detail": "Current acquisition failures",
            },
            {
                "key": "timeliness",
                "dimension": "Timeliness",
                "status": "Warning" if failed else "Normal",
                "metric": f"{timeout_count} timeout tasks",
                "detail": "Current runtime state",
            },
            {
                "key": "completeness",
                "dimension": "Completeness",
                "status": (
                    "Normal"
                    if completeness >= 99.9
                    else "Warning"
                ),
                "metric": f"{completeness:.2f}%",
                "detail": f"{missing} missing cycles",
            },
            {
                "key": "validity",
                "dimension": "Validity",
                "status": "Warning" if bad else "Normal",
                "metric": f"{bad} BAD points",
                "detail": f"{received} received samples",
            },
            {
                "key": "delivery",
                "dimension": "Delivery Integrity",
                "status": (
                    "Fault"
                    if self._runtime.points_dropped
                    else "Normal"
                ),
                "metric": (
                    f"{self._runtime.points_dropped} "
                    "dropped points"
                ),
                "detail": "Runtime backpressure counter",
            },
        ]

        issues = [
            {
                "level": "Fault",
                "object": state.instance_id,
                "kind": "Task",
                "dimension": "Continuity",
                "issue": "Collection failed",
                "duration": "Active",
                "error": state.last_error or "collection failure",
            }
            for state in failed
        ]
        issues.extend(
            {
                "level": "Fault",
                "object": device_id,
                "kind": "Device",
                "dimension": "Timeliness",
                "issue": "Device disconnected",
                "duration": "Active",
                "error": (
                    self._runtime.device_state(device_id).last_error
                    if self._runtime.device_state(device_id) is not None
                    else "protocol unhealthy"
                )
                or "protocol unhealthy",
            }
            for device_id in offline
        )

        acquisition_channels = []
        for device_id, device in self._runtime.devices.items():
            state = self._runtime.device_state(device_id)
            healthy = device.health().healthy
            acquisition_channels.append(
                {
                    "object": device_id,
                    "source": "Acquisition",
                    "protocol": device.config.protocol.upper(),
                    "state": "Healthy" if healthy else "Interrupted",
                    "target": device.config.endpoint.host,
                    "last": "—",
                    "latency": "—",
                    "timeouts": (
                        1
                        if state is not None
                        and "timeout" in (state.last_error or "").lower()
                        else 0
                    ),
                    "reconnects": (
                        state.consecutive_failures
                        if state is not None
                        else 0
                    ),
                    "issue": (
                        "—"
                        if healthy
                        else (
                            state.last_error
                            if state is not None
                            else "Protocol unhealthy"
                        )
                    ),
                }
            )

        delivery_channels = []
        queue_depths = self._runtime.sink_queue_depths()
        for name, sink in self._runtime.sinks.items():
            health = sink.health()
            delivery_channels.append(
                {
                    "object": name,
                    "source": "Delivery",
                    "protocol": "SINK",
                    "state": "Healthy" if health.healthy else "Interrupted",
                    "target": name,
                    "last": "—",
                    "latency": "—",
                    "timeouts": 0,
                    "reconnects": 0,
                    "issue": health.message or ("—" if health.healthy else "Sink unhealthy"),
                    "queue_depth": queue_depths.get(name, 0),
                }
            )

        communication_events = [
            {
                "id": index + 1,
                "time": datetime.now(UTC).isoformat(),
                "object": state.instance_id,
                "protocol": "RUNTIME",
                "event": "Collection failure",
                "state": "Active",
                "error": state.last_error or "collection failure",
                "duration": "Active",
                "target": state.device_id,
            }
            for index, state in enumerate(failed)
        ]

        return {
            "window": window,
            "channelSummary": [
                {
                    "key": "interrupted",
                    "label": "Interrupted",
                    "value": len(offline),
                    "tone": "danger",
                },
                {
                    "key": "degraded",
                    "label": "Degraded",
                    "value": len(failed),
                    "tone": "warning",
                },
                {
                    "key": "timeouts",
                    "label": "Timeouts",
                    "value": timeout_count,
                    "tone": "warning",
                },
                {
                    "key": "reconnects",
                    "label": "Reconnects",
                    "value": reconnects,
                    "tone": "warning",
                },
            ],
            "communicationEvents": communication_events,
            "acquisitionChannels": acquisition_channels,
            "deliveryChannels": delivery_channels,
            "dataMetrics": [
                {
                    "key": "stale",
                    "label": "Stale Tasks",
                    "value": len(failed),
                    "hint": "current",
                    "tone": "danger",
                },
                {
                    "key": "missing",
                    "label": "Missing Cycles",
                    "value": missing,
                    "hint": f"{received}/{expected} samples",
                    "tone": "warning",
                },
                {
                    "key": "reads",
                    "label": "Point Read Failures",
                    "value": bad,
                    "hint": window,
                    "tone": "warning",
                },
                {
                    "key": "dropped",
                    "label": "Dropped Points",
                    "value": self._runtime.points_dropped,
                    "hint": "sink backpressure",
                    "tone": "danger",
                },
            ],
            "dimensions": dimensions,
            "issues": issues,
        }

    def _expected_samples(
        self,
        cfg: Config,
        seconds: int,
    ) -> int:
        expected = 0
        for task in cfg.tasks.tasks:
            if not task.enabled or not task.interval:
                continue
            targets = [
                device
                for device in cfg.devices.devices
                if (
                    task.device == device.device_id
                    or task.device_group == device.device_group
                )
                and device.enabled
            ]
            for device in targets:
                points = [
                    point
                    for point in cfg.points_for_device(
                        device.device_id
                    )
                    if task.point_group
                    in point.point_groups
                ]
                expected += (
                    int(seconds / task.interval)
                    * len(points)
                )
        return expected


class SystemHealthUseCase:
    """读取 Linux/openEuler 主机与进程资源并保留短期历史。"""

    WINDOWS = {
        "1 h": 3600,
        "24 h": 86400,
        "7 d": 604800,
        "30 d": 2592000,
    }

    def __init__(self) -> None:
        self._samples: deque[dict[str, Any]] = deque(
            maxlen=10000
        )

    def snapshot(self, window: str) -> dict[str, Any]:
        sample = self._sample()
        self._samples.append(sample)
        cutoff = datetime.now(UTC) - timedelta(
            seconds=self.WINDOWS.get(window, 86400)
        )
        rows = [
            row
            for row in self._samples
            if row["time"] >= cutoff
        ]
        return {
            "current": sample,
            "series": rows,
            "mounts": self._mounts(),
            "risks": self._risks(sample),
        }

    def _sample(self) -> dict[str, Any]:
        mem_total = 0
        mem_available = 0
        try:
            for line in Path(
                "/proc/meminfo"
            ).read_text().splitlines():
                if line.startswith("MemTotal:"):
                    mem_total = int(
                        line.split()[1]
                    ) * 1024
                elif line.startswith("MemAvailable:"):
                    mem_available = int(
                        line.split()[1]
                    ) * 1024
        except OSError:
            pass

        rss = (
            resource.getrusage(
                resource.RUSAGE_SELF
            ).ru_maxrss
            * 1024
        )
        load1 = (
            os.getloadavg()[0]
            if hasattr(os, "getloadavg")
            else 0.0
        )
        root_usage = shutil.disk_usage("/")
        temperature = self._cpu_temperature()
        return {
            "time": datetime.now(UTC),
            "memory_total": mem_total,
            "memory_used": max(
                0,
                mem_total - mem_available,
            ),
            "process_rss": rss,
            "load1": load1,
            "cpu_count": os.cpu_count() or 1,
            "root_disk_free": root_usage.free,
            "cpu_temperature": temperature,
        }

    def _cpu_temperature(self) -> float | None:
        """读取 Linux thermal zone 温度；无传感器时返回 None。"""
        for path in sorted(
            Path("/sys/class/thermal").glob(
                "thermal_zone*/temp"
            )
        ):
            try:
                raw = float(path.read_text().strip())
            except (OSError, ValueError):
                continue
            value = raw / 1000 if raw > 1000 else raw
            if 0 < value < 150:
                return round(value, 1)
        return None

    def _mounts(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for mount in ("/", "/var", "/tmp"):
            if not Path(mount).exists():
                continue
            usage = shutil.disk_usage(mount)
            rows.append(
                {
                    "mount": mount,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "usage_pct": (
                        0
                        if usage.total == 0
                        else round(
                            usage.used
                            / usage.total
                            * 100,
                            1,
                        )
                    ),
                }
            )
        return rows

    def _risks(
        self,
        sample: dict[str, Any],
    ) -> list[dict[str, str]]:
        risks: list[dict[str, str]] = []
        if (
            sample["memory_total"]
            and sample["memory_used"]
            / sample["memory_total"]
            > 0.9
        ):
            risks.append(
                {
                    "name": "Memory",
                    "state": "WARNING",
                    "summary": (
                        "Host memory usage > 90%"
                    ),
                    "detail": (
                        "Check process RSS and workload"
                    ),
                }
            )
        for mount in self._mounts():
            if mount["usage_pct"] > 90:
                risks.append(
                    {
                        "name": "Storage",
                        "state": "WARNING",
                        "summary": (
                            f"{mount['mount']} usage > 90%"
                        ),
                        "detail": "Free space is low",
                    }
                )
        return risks
