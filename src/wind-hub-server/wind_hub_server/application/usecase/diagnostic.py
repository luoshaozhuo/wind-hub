"""网络、协议与设备读写诊断用例。"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel

from wind_hub_server.application.operation import OperationManager, OperationRecord
from wind_hub.application.runtime.runtime import Runtime
from wind_hub_server.application.usecase.device_control import DeviceCommandResult, DeviceControlUseCase
from wind_hub.application.usecase.query import QueryUseCase
from wind_hub_core.model.point import PointValue
from wind_hub_server.infra.network_probe import expand_network, ping_host, probe_port


class PingResult(BaseModel):
    host: str
    reachable: bool
    latency_ms: float


class PortResult(BaseModel):
    port: int
    state: str
    latency_ms: float


class DiagnosticUseCase:
    """Diagnostics 页的真实执行入口。"""

    def __init__(
        self,
        runtime: Runtime,
        query: QueryUseCase,
        control: DeviceControlUseCase,
        operations: OperationManager,
    ) -> None:
        self._runtime = runtime
        self._query = query
        self._control = control
        self._operations = operations
        self._background: set[asyncio.Task[None]] = set()

    async def ping(self, host: str, timeout: float = 1.0) -> PingResult:
        result = await ping_host(host, timeout)
        return PingResult(
            host=result.host,
            reachable=result.reachable,
            latency_ms=result.latency_ms,
        )

    async def ports(
        self, host: str, ports: list[int], timeout: float = 1.0
    ) -> list[PortResult]:
        results = await asyncio.gather(*(probe_port(host, port, timeout) for port in ports))
        return [
            PortResult(
                port=result.port,
                state=result.state,
                latency_ms=result.latency_ms,
            )
            for result in results
        ]

    async def protocol_check(self, device_id: str) -> bool:
        """确认已配置设备协议连接可用，必要时触发 Runtime 重连。"""
        if device_id not in self._runtime.devices:
            raise KeyError(device_id)
        return await self._runtime.ensure_connected(device_id)

    async def read(self, device_id: str, point_id: str) -> PointValue:
        """直接读设备单点，绕过采集缓存。"""
        return await self._query.read_point(device_id, point_id)

    async def write(
        self, device_id: str, point_id: str, value: Any
    ) -> DeviceCommandResult:
        """诊断写复用正式 DeviceControlUseCase。"""
        return await self._control.send(device_id, point_id, value)

    def start_subnet_scan(
        self, network: str, *, timeout: float = 0.5, ports: list[int] | None = None
    ) -> OperationRecord:
        """创建异步子网扫描 Operation。"""
        ips = expand_network(network)
        operation = self._operations.create("diagnostics.subnet_scan", total=len(ips))
        task = asyncio.create_task(
            self._scan_worker(operation.operation_id, ips, timeout, ports or [502, 2404, 48898])
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return operation

    def start_point_table_test(self, device_id: str) -> OperationRecord:
        """逐点真实读取当前设备点表，异步返回成功/失败明细。"""
        device = self._runtime.devices.get(device_id)
        if device is None:
            raise KeyError(device_id)
        operation = self._operations.create(
            "diagnostics.point_table", total=len(device.points)
        )
        task = asyncio.create_task(
            self._point_table_worker(
                operation.operation_id,
                device_id,
                [point.point_id for point in device.points],
            )
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return operation

    async def _point_table_worker(
        self, operation_id: str, device_id: str, point_ids: list[str]
    ) -> None:
        self._operations.mark_running(operation_id)
        rows: list[dict[str, object]] = []
        failures = 0
        for index, point_id in enumerate(point_ids, start=1):
            try:
                value = await self._query.read_point(device_id, point_id)
            except Exception as exc:
                failures += 1
                rows.append(
                    {
                        "point_id": point_id,
                        "success": False,
                        "error": str(exc) or type(exc).__name__,
                    }
                )
            else:
                rows.append(
                    {
                        "point_id": point_id,
                        "success": True,
                        "quality": value.quality.value,
                        "value": value.value,
                    }
                )
            self._operations.update_progress(operation_id, completed=index)
        result: dict[str, object] = {"points": rows, "failed": failures}
        if failures == 0:
            self._operations.succeed(operation_id, result)
        elif failures < len(point_ids):
            self._operations.complete_partial(operation_id, result)
        else:
            self._operations.fail(
                operation_id,
                code="POINT_TABLE_VERIFY_FAILED",
                message="all point reads failed",
                details=result,
            )

    async def _scan_worker(
        self, operation_id: str, ips: list[str], timeout: float, ports: list[int]
    ) -> None:
        self._operations.mark_running(operation_id)
        hosts: list[dict[str, object]] = []
        try:
            for index in range(0, len(ips), 64):
                chunk = ips[index : index + 64]
                rows = await asyncio.gather(
                    *(self._scan_host(ip, timeout, ports) for ip in chunk)
                )
                hosts.extend(row for row in rows if row is not None)
                self._operations.update_progress(
                    operation_id, completed=min(index + len(chunk), len(ips))
                )
            self._operations.succeed(operation_id, {"hosts": hosts})
        except Exception as exc:
            self._operations.fail(
                operation_id, code="DIAGNOSTIC_SCAN_FAILED", message=str(exc)
            )

    async def _scan_host(
        self, host: str, timeout: float, ports: list[int]
    ) -> dict[str, object] | None:
        ping = await ping_host(host, timeout)
        probed = await asyncio.gather(*(probe_port(host, port, timeout) for port in ports))
        open_ports = [row.port for row in probed if row.state == "open"]
        if not ping.reachable and not open_ports:
            return None
        return {
            "ip": host,
            "reachable": ping.reachable,
            "open_ports": open_ports,
        }
