"""网络、协议与设备读写诊断服务。"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from pydantic import BaseModel

from core.domain.identities import DeviceId
from web_server.application.config.service import ConfigService
from web_server.application.device.command import (
    DeviceCommandResult,
    DeviceCommandService,
)
from web_server.application.device.sample import PointValue
from web_server.application.operation.registry import OperationRecord, OperationRegistry
from web_server.application.port.network_probe import NetworkProbePort
from web_server.application.port.worker import CommanderPort


class PingResult(BaseModel):
    host: str
    reachable: bool
    latency_ms: float


class PortResult(BaseModel):
    port: int
    state: str
    latency_ms: float


class DiagnosticService:
    """Diagnostics 页执行入口。

    Server 自己执行网络层 ping/port scan；所有协议级设备访问通过 Commander，
    避免 Server/Collector 重复维护 PLC 会话。
    """

    def __init__(
        self,
        commander: CommanderPort,
        control: DeviceCommandService,
        config: ConfigService,
        operations: OperationRegistry,
        network: NetworkProbePort,
    ) -> None:
        self._commander = commander
        self._control = control
        self._config = config
        self._operations = operations
        self._network = network
        self._background: set[asyncio.Task[None]] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}

    def cancel_operation(self, operation_id: str) -> OperationRecord:
        """取消运行中的诊断 Operation。

        未知 ID 抛 KeyError；已终态抛 ValueError。取消立即把 Operation 置为
        cancelled，并取消底层协程；worker 内的兜底取消标记与终态保护冲突时
        静默忽略。
        """
        self._operations.get(operation_id)  # 未知 ID 抛 KeyError
        task = self._tasks.get(operation_id)
        if task is not None:
            task.cancel()
        return self._operations.cancel(operation_id)

    async def aclose(self) -> None:
        """进程关闭时取消所有后台诊断任务并等待退出。"""
        tasks = list(self._background)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _spawn(self, operation_id: str, coro: Any) -> None:
        """登记后台任务，支持按 Operation ID 取消。"""
        task = asyncio.create_task(coro)
        self._background.add(task)
        self._tasks[operation_id] = task

        def _forget(done: asyncio.Task[None]) -> None:
            self._background.discard(done)
            self._tasks.pop(operation_id, None)

        task.add_done_callback(_forget)

    async def ping(self, host: str, timeout: float = 1.0) -> PingResult:
        result = await self._network.ping(host, timeout)
        return PingResult(
            host=result.host,
            reachable=result.reachable,
            latency_ms=result.latency_ms,
        )

    async def ports(
        self,
        host: str,
        ports: list[int],
        timeout: float = 1.0,
    ) -> list[PortResult]:
        results = await asyncio.gather(
            *(self._network.probe_port(host, port, timeout) for port in ports)
        )
        return [
            PortResult(
                port=result.port,
                state=result.state,
                latency_ms=result.latency_ms,
            )
            for result in results
        ]

    async def protocol_check(self, device_id: str) -> bool:
        """由 Commander 验证网络/TCP/协议会话，返回协议阶段结果。"""
        result = await self._commander.verify_device(device_id)
        for stage in result.stages:
            if stage.name == "protocol":
                return stage.ok
        return result.ok

    async def read(self, device_id: str, point_id: str) -> PointValue:
        """通过 Commander 即时读取单点，绕过采集缓存。"""
        return await self._commander.read_point(device_id, point_id)

    async def write(
        self,
        device_id: str,
        point_id: str,
        value: Any,
    ) -> DeviceCommandResult:
        """诊断写复用正式 DeviceCommandService。"""
        return await self._control.send(device_id, point_id, value)

    def start_subnet_scan(
        self,
        network: str,
        *,
        timeout: float = 0.5,
        ports: list[int] | None = None,
    ) -> OperationRecord:
        """创建异步子网扫描 Operation。"""
        ips = self._network.expand_network(network)
        operation = self._operations.create(
            "diagnostics.subnet_scan",
            total=len(ips),
        )
        self._spawn(
            operation.operation_id,
            self._scan_worker(
                operation.operation_id,
                ips,
                timeout,
                ports or [502, 2404, 48898],
            ),
        )
        return operation

    def start_point_table_test(self, device_id: str) -> OperationRecord:
        """按当前 Server 配置取得点表，逐点通过 Commander 在线验证。"""
        cfg = self._config.current_config
        device = cfg.site.devices.get(DeviceId(device_id))
        if device is None:
            raise KeyError(device_id)
        model = cfg.device_models[device.device_model_id]
        point_ids = list(cfg.point_tables[model.point_table_id].points)
        operation = self._operations.create(
            "diagnostics.point_table",
            total=len(point_ids),
        )
        self._spawn(
            operation.operation_id,
            self._point_table_worker(
                operation.operation_id,
                device_id,
                point_ids,
            ),
        )
        return operation

    async def _point_table_worker(
        self,
        operation_id: str,
        device_id: str,
        point_ids: list[str],
    ) -> None:
        self._operations.mark_running(operation_id)
        try:
            rows, failures = await self._verify_points(operation_id, device_id, point_ids)
        except asyncio.CancelledError:
            # 取消通常已被 cancel_operation 标记；终态保护冲突时静默。
            with contextlib.suppress(ValueError, KeyError):
                self._operations.cancel(operation_id)
            raise

        summary: dict[str, object] = {"points": rows, "failed": failures}
        if failures == 0:
            self._operations.succeed(operation_id, summary)
        elif failures < len(point_ids):
            self._operations.complete_partial(operation_id, summary)
        else:
            self._operations.fail(
                operation_id,
                code="POINT_TABLE_VERIFY_FAILED",
                message="all point reads failed",
                details=summary,
            )

    async def _verify_points(
        self,
        operation_id: str,
        device_id: str,
        point_ids: list[str],
    ) -> tuple[list[dict[str, object]], int]:
        """逐点在线验证；单点失败不中断，整体取消经 CancelledError 上抛。"""
        rows: list[dict[str, object]] = []
        failures = 0
        for index, point_id in enumerate(point_ids, start=1):
            try:
                result = await self._commander.verify_point(device_id, point_id)
                success = result.ok
                if not success:
                    failures += 1
                rows.append(
                    {
                        "point_id": point_id,
                        "success": success,
                        "quality": result.quality,
                        "value": result.engineering_value,
                        "error": result.error,
                    }
                )
            except Exception as exc:
                failures += 1
                rows.append(
                    {
                        "point_id": point_id,
                        "success": False,
                        "error": str(exc) or type(exc).__name__,
                    }
                )
            # 取消竞态：cancel_operation 先置终态再取消协程，进度更新落在
            # 终态之后属无害冲突，静默忽略并等待 CancelledError 生效。
            with contextlib.suppress(ValueError, KeyError):
                self._operations.update_progress(operation_id, completed=index)
        return rows, failures

    async def _scan_worker(
        self,
        operation_id: str,
        ips: list[str],
        timeout: float,
        ports: list[int],
    ) -> None:
        self._operations.mark_running(operation_id)
        hosts: list[dict[str, object]] = []
        try:
            for index in range(0, len(ips), 64):
                chunk = ips[index : index + 64]
                rows = await asyncio.gather(*(self._scan_host(ip, timeout, ports) for ip in chunk))
                hosts.extend(row for row in rows if row is not None)
                self._operations.update_progress(
                    operation_id,
                    completed=min(index + len(chunk), len(ips)),
                )
            self._operations.succeed(operation_id, {"hosts": hosts})
        except asyncio.CancelledError:
            with contextlib.suppress(ValueError, KeyError):
                self._operations.cancel(operation_id)
            raise
        except Exception as exc:
            self._operations.fail(
                operation_id,
                code="DIAGNOSTIC_SCAN_FAILED",
                message=str(exc),
            )

    async def _scan_host(
        self,
        host: str,
        timeout: float,
        ports: list[int],
    ) -> dict[str, object] | None:
        ping = await self._network.ping(host, timeout)
        probed = await asyncio.gather(
            *(self._network.probe_port(host, port, timeout) for port in ports)
        )
        open_ports = [row.port for row in probed if row.state == "open"]
        if not ping.reachable and not open_ports:
            return None
        return {
            "ip": host,
            "reachable": ping.reachable,
            "open_ports": open_ports,
        }
