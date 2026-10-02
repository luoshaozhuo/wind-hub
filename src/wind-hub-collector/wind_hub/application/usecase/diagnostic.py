"""Collector 独立现场诊断用例。

诊断由控制面显式触发，不参与 Collector 启动、周期采集或配置热重载。
它复用当前 Runtime 的设备对象和 wind-hub-core 主动探测能力，分别验证
网络/TCP/协议链路、ADS 地址解析和点位实际读取。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from wind_hub.application.runtime.runtime import Runtime
from wind_hub.config.schema import PointConfig
from wind_hub.domain.model.errors import CommandError
from wind_hub.domain.model.point import PointRef, Quality
from wind_hub_core.protocol.ads import ADSProbe
from wind_hub_core.validation.models import DeviceProbeTarget, PointProbeSpec
from wind_hub_core.validation.network import ping_host, tcp_port_open


class DiagnosticStage(BaseModel):
    """设备链路中的一个独立验证阶段。"""

    name: str
    ok: bool
    message: str = ""


class DeviceVerifyResult(BaseModel):
    """设备通信链路验证结果。"""

    device_id: str
    protocol: str
    host: str
    port: int
    ok: bool
    stages: list[DiagnosticStage] = Field(default_factory=list)


class PointVerifyResult(BaseModel):
    """单点在线验证结果，同时保留协议原始值与工程值。"""

    device_id: str
    point_id: str
    variable_name: str | None = None
    protocol: str
    configured_address: dict[str, Any]
    resolved_address: dict[str, Any] | None = None
    data_type: str
    scale: float
    offset: float
    unit: str
    readable: bool | None = None
    raw_value: Any = None
    engineering_value: Any = None
    quality: str | None = None
    source: str | None = None
    error: str | None = None


class PointsVerifyResult(BaseModel):
    """一批点位的在线验证结果。"""

    device_id: str
    point_group: str | None = None
    checked: int
    passed: int
    failed: int
    points: list[PointVerifyResult]


class DiagnosticUseCase:
    """Collector 的按需现场诊断入口。"""

    def __init__(self, runtime: Runtime) -> None:
        self._runtime = runtime

    async def verify_device(
        self,
        device_id: str,
        *,
        timeout: float = 1.0,
    ) -> DeviceVerifyResult:
        """验证 ICMP、TCP 和当前协议会话三层通信事实。

        Args:
            device_id: Runtime 中的设备标识。
            timeout: ICMP/TCP 单阶段探测超时，单位秒。

        Returns:
            分层设备链路结果。

        Raises:
            CommandError: 设备不存在。
        """
        device = self._device(device_id)
        cfg = device.config
        ping_ok = await ping_host(cfg.endpoint.host, timeout=timeout)
        port_ok = await tcp_port_open(
            cfg.endpoint.host,
            cfg.endpoint.port,
            timeout=timeout,
        )
        stages = [
            DiagnosticStage(
                name="network",
                ok=ping_ok,
                message="" if ping_ok else "ICMP ping failed or is blocked",
            ),
            DiagnosticStage(
                name="transport",
                ok=port_ok,
                message=(
                    f"TCP {cfg.endpoint.port} reachable"
                    if port_ok
                    else f"TCP {cfg.endpoint.port} unreachable"
                ),
            ),
        ]

        protocol_ok = False
        protocol_message = ""
        if port_ok:
            if device.health().healthy:
                protocol_ok = True
                protocol_message = "protocol session is healthy"
            else:
                protocol_ok = await self._runtime.ensure_connected(device_id)
                if protocol_ok:
                    protocol_message = "protocol reconnect succeeded"
                else:
                    state = self._runtime.device_state(device_id)
                    protocol_message = (
                        state.last_error
                        if state is not None and state.last_error
                        else "protocol session unavailable"
                    )
        else:
            protocol_message = "protocol check skipped because TCP is unreachable"
        stages.append(
            DiagnosticStage(
                name="protocol",
                ok=protocol_ok,
                message=protocol_message,
            )
        )
        return DeviceVerifyResult(
            device_id=device_id,
            protocol=cfg.protocol,
            host=cfg.endpoint.host,
            port=cfg.endpoint.port,
            ok=port_ok and protocol_ok,
            stages=stages,
        )

    async def resolve_point(
        self,
        device_id: str,
        point_id: str,
    ) -> PointVerifyResult:
        """解析点位协议地址；ADS 会实际查询 symbol 信息但不读取点值。"""
        point = self._point(device_id, point_id)
        resolved, errors = await self._resolve_addresses(device_id, [point])
        return self._point_result(
            device_id,
            point,
            resolved.get(point.point_id),
            readable=None,
            error=errors.get(point.point_id),
        )

    async def verify_point(
        self,
        device_id: str,
        point_id: str,
    ) -> PointVerifyResult:
        """实际读取单点并返回 raw/engineering value 与地址信息。"""
        point = self._point(device_id, point_id)
        resolved, errors = await self._resolve_addresses(device_id, [point])
        rows = await self._verify_read(device_id, [point], resolved, errors)
        return rows[0]

    async def verify_points(
        self,
        device_id: str,
        *,
        point_group: str | None = None,
    ) -> PointsVerifyResult:
        """批量验证整个设备点表或指定 point_group。"""
        device = self._device(device_id)
        points = (
            device.point_group_points(point_group)
            if point_group is not None
            else list(device.points)
        )
        if point_group is not None and not points:
            raise CommandError(
                f"unknown or empty point group '{device_id}/{point_group}'",
                "",
            )
        resolved, errors = await self._resolve_addresses(device_id, points)
        rows = await self._verify_read(device_id, points, resolved, errors)
        passed = sum(1 for row in rows if row.readable)
        return PointsVerifyResult(
            device_id=device_id,
            point_group=point_group,
            checked=len(rows),
            passed=passed,
            failed=len(rows) - passed,
            points=rows,
        )

    def _device(self, device_id: str):
        device = self._runtime.devices.get(device_id)
        if device is None:
            raise CommandError(f"unknown device '{device_id}'", "")
        return device

    def _point(self, device_id: str, point_id: str) -> PointConfig:
        device = self._device(device_id)
        for point in device.points:
            if point.point_id == point_id:
                return point
        raise CommandError(f"unknown point '{device_id}/{point_id}'", "")

    async def _resolve_addresses(
        self,
        device_id: str,
        points: list[PointConfig],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
        """返回协议地址事实和逐点解析错误。

        ADS 在同一短生命周期 Probe 内逐点解析；单点失败不会终止整批。
        """
        device = self._device(device_id)
        if device.config.protocol != "ads":
            return (
                {
                    point.point_id: point.address.model_dump(
                        mode="json",
                        exclude_none=True,
                    )
                    for point in points
                },
                {},
            )

        target = DeviceProbeTarget(
            device_id=device_id,
            host=device.config.endpoint.host,
            options=dict(device.config.endpoint.extensions),
        )
        probe = ADSProbe(target)
        resolved_rows: dict[str, dict[str, Any]] = {}
        errors: dict[str, str] = {}
        try:
            await probe.connect()
            for point in points:
                spec = PointProbeSpec(
                    point_id=point.point_id,
                    data_type=point.data_type,
                    address=point.address.model_dump(
                        mode="python",
                        exclude_none=True,
                    ),
                )
                try:
                    resolved = await probe.resolve_points([spec])
                except Exception as exc:
                    errors[point.point_id] = str(exc) or type(exc).__name__
                    continue
                item = resolved[point.point_id]
                resolved_rows[point.point_id] = {
                    "symbol": item.symbol,
                    "index_group": item.index_group,
                    "index_offset": item.index_offset,
                    "size": item.size,
                    "protocol_type": item.protocol_type,
                }
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            for point in points:
                errors.setdefault(point.point_id, error)
        finally:
            await probe.close()
        return resolved_rows, errors

    async def _verify_read(
        self,
        device_id: str,
        points: list[PointConfig],
        resolved: dict[str, dict[str, Any]],
        address_errors: dict[str, str],
    ) -> list[PointVerifyResult]:
        """使用当前 Runtime Driver 批量读取原始值，并逐点形成诊断结果。"""
        device = self._device(device_id)
        if not await self._runtime.ensure_connected(device_id):
            error = "device protocol session is unavailable"
            return [
                self._point_result(
                    device_id,
                    point,
                    resolved.get(point.point_id),
                    readable=False,
                    error=address_errors.get(point.point_id, error),
                )
                for point in points
            ]

        readable_points = [
            point for point in points if point.point_id not in address_errors
        ]
        refs = [
            PointRef(device_id=device_id, point_id=point.point_id)
            for point in readable_points
        ]
        if not refs:
            return [
                self._point_result(
                    device_id,
                    point,
                    resolved.get(point.point_id),
                    readable=False,
                    error=address_errors.get(point.point_id),
                )
                for point in points
            ]
        try:
            values = await device.protocol.read(refs)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            return [
                self._point_result(
                    device_id,
                    point,
                    resolved.get(point.point_id),
                    readable=False,
                    error=address_errors.get(point.point_id, error),
                )
                for point in points
            ]

        by_id = {value.point_id: value for value in values}
        rows: list[PointVerifyResult] = []
        for point in points:
            address_error = address_errors.get(point.point_id)
            if address_error is not None:
                rows.append(
                    self._point_result(
                        device_id,
                        point,
                        resolved.get(point.point_id),
                        readable=False,
                        error=address_error,
                    )
                )
                continue
            value = by_id.get(point.point_id)
            if value is None:
                rows.append(
                    self._point_result(
                        device_id,
                        point,
                        resolved.get(point.point_id),
                        readable=False,
                        error="protocol returned no value",
                    )
                )
                continue
            readable = value.quality is not Quality.BAD and value.value is not None
            rows.append(
                self._point_result(
                    device_id,
                    point,
                    resolved.get(point.point_id),
                    readable=readable,
                    raw_value=value.value,
                    quality=value.quality.value,
                    source=value.source,
                    error=None if readable else "point quality is bad or value is null",
                )
            )
        return rows

    def _point_result(
        self,
        device_id: str,
        point: PointConfig,
        resolved_address: dict[str, Any] | None,
        *,
        readable: bool | None,
        raw_value: Any = None,
        quality: str | None = None,
        source: str | None = None,
        error: str | None = None,
    ) -> PointVerifyResult:
        engineering = raw_value
        if (
            isinstance(raw_value, int | float)
            and not isinstance(raw_value, bool)
        ):
            engineering = raw_value * point.scale + point.offset
        device = self._device(device_id)
        return PointVerifyResult(
            device_id=device_id,
            point_id=point.point_id,
            variable_name=point.variable_name,
            protocol=device.config.protocol,
            configured_address=point.address.model_dump(
                mode="json",
                exclude_none=True,
            ),
            resolved_address=resolved_address,
            data_type=point.data_type,
            scale=point.scale,
            offset=point.offset,
            unit=point.unit,
            readable=readable,
            raw_value=raw_value,
            engineering_value=engineering,
            quality=quality,
            source=source,
            error=error,
        )
