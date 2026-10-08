"""Commander 独立现场诊断服务。

诊断由控制面显式触发，不参与采集或配置热重载。它复用当前 Runtime 的
设备会话，分层验证网络/TCP/协议链路、协议地址解析和点位实际读取。

ADS 的 symbol 地址解析经注入的 probe factory 执行（短生命周期验证会话，
实现位于 Infrastructure）；其他协议直接以配置地址作为解析事实。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from core.application import Quality
from core.domain import ConnectionEndpoint, Device, Point, ProtocolOptions

from .errors import CommandError
from .runtime import CommanderRuntime
from .session import DeviceSession, engineering_value


class DiagnosticCode(StrEnum):
    """诊断阶段的稳定结果码（gRPC/CLI 契约）。"""

    OK = "OK"
    PING_FAILED = "PING_FAILED"
    TCP_PORT_UNREACHABLE = "TCP_PORT_UNREACHABLE"
    PROTOCOL_CONNECT_FAILED = "PROTOCOL_CONNECT_FAILED"
    POINT_RESOLVE_FAILED = "POINT_RESOLVE_FAILED"
    POINT_READ_FAILED = "POINT_READ_FAILED"
    POINT_MAPPING_MISMATCH = "POINT_MAPPING_MISMATCH"


class DiagnosticSeverity(StrEnum):
    """诊断阶段严重级别。"""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class DiagnosticStage:
    """设备链路中的一个独立验证阶段。"""

    name: str
    ok: bool
    code: DiagnosticCode
    severity: DiagnosticSeverity
    message: str = ""


@dataclass(frozen=True, slots=True)
class DeviceVerifyResult:
    """设备通信链路分层验证结果。"""

    device_id: str
    protocol: str
    host: str
    port: int | None
    ok: bool
    stages: tuple[DiagnosticStage, ...] = ()


@dataclass(frozen=True, slots=True)
class PointVerifyResult:
    """单点在线验证结果，同时保留协议原始值与工程值。"""

    device_id: str
    point_id: str
    variable_name: str | None
    protocol: str
    configured_address: dict[str, Any]
    resolved_address: dict[str, Any] | None
    data_type: str
    scale: float
    offset: float
    unit: str
    readable: bool | None = None
    ok: bool = False
    code: DiagnosticCode = DiagnosticCode.OK
    severity: DiagnosticSeverity = DiagnosticSeverity.INFO
    raw_value: Any = None
    engineering_value: Any = None
    quality: str | None = None
    source: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PointsVerifyResult:
    """一批点位的在线验证结果。"""

    device_id: str
    point_group: str | None
    checked: int
    passed: int
    failed: int
    ok: bool
    points: tuple[PointVerifyResult, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedAddress:
    """协议地址解析事实（ADS 为 symbol 绑定的 session 地址）。"""

    symbol: str | None
    index_group: int
    index_offset: int
    size: int | None
    protocol_type: str


class SymbolProbe(Protocol):
    """短生命周期协议地址解析/验证会话（ADS）。"""

    async def connect(self) -> None: ...
    async def close(self) -> None: ...

    async def resolve(self, point: Point) -> ResolvedAddress:
        """解析单点地址；symbol 存在时实际查询 PLC。"""
        ...

    async def read_raw(self, point: Point, resolved: ResolvedAddress) -> object:
        """按解析地址读取一次协议原始值。"""
        ...


#: 组合根注入的 probe 工厂；返回 None 表示该协议无主动解析 probe。
SymbolProbeFactory = Callable[
    [Device, ConnectionEndpoint, ProtocolOptions],
    SymbolProbe | None,
]

#: 网络探测函数注入点（ping / tcp），便于测试替换。
PingProbe = Callable[[str, float], Awaitable[bool]]
TcpProbe = Callable[[str, int, float], Awaitable[bool]]


class CommanderDiagnosticService:
    """Commander 的按需现场诊断入口。"""

    def __init__(
        self,
        runtime: CommanderRuntime,
        *,
        ping: PingProbe,
        tcp_connect: TcpProbe,
        probe_factory: SymbolProbeFactory | None = None,
    ) -> None:
        self._runtime = runtime
        self._probe_factory = probe_factory
        self._ping = ping
        self._tcp = tcp_connect

    async def verify_device(
        self,
        device_id: str,
        *,
        timeout: float = 1.0,
    ) -> DeviceVerifyResult:
        """验证 ICMP、TCP 和当前协议会话三层通信事实。"""
        async with self._runtime.operation():
            return await self._verify_device(device_id, timeout=timeout)

    async def _verify_device(
        self,
        device_id: str,
        *,
        timeout: float,
    ) -> DeviceVerifyResult:
        device = self._device(device_id)
        endpoint = device.device.endpoint
        ping_ok = await self._ping(endpoint.host, timeout)
        port = endpoint.port or 0
        port_ok = bool(port) and await self._tcp(endpoint.host, port, timeout)
        stages = [
            DiagnosticStage(
                name="network",
                ok=ping_ok,
                code=(DiagnosticCode.OK if ping_ok else DiagnosticCode.PING_FAILED),
                severity=(DiagnosticSeverity.INFO if ping_ok else DiagnosticSeverity.WARNING),
                message="" if ping_ok else "ICMP ping failed or is blocked",
            ),
            DiagnosticStage(
                name="transport",
                ok=port_ok,
                code=(DiagnosticCode.OK if port_ok else DiagnosticCode.TCP_PORT_UNREACHABLE),
                severity=(DiagnosticSeverity.INFO if port_ok else DiagnosticSeverity.ERROR),
                message=(f"TCP {port} reachable" if port_ok else f"TCP {port} unreachable"),
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
                protocol_message = (
                    "protocol reconnect succeeded"
                    if protocol_ok
                    else "protocol session unavailable"
                )
        else:
            protocol_message = "protocol check skipped because TCP is unreachable"
        stages.append(
            DiagnosticStage(
                name="protocol",
                ok=protocol_ok,
                code=(DiagnosticCode.OK if protocol_ok else DiagnosticCode.PROTOCOL_CONNECT_FAILED),
                severity=(DiagnosticSeverity.INFO if protocol_ok else DiagnosticSeverity.ERROR),
                message=protocol_message,
            )
        )
        return DeviceVerifyResult(
            device_id=device_id,
            protocol=device.protocol_name,
            host=endpoint.host,
            port=endpoint.port,
            ok=port_ok and protocol_ok,
            stages=tuple(stages),
        )

    async def resolve_point(
        self,
        device_id: str,
        point_id: str,
    ) -> PointVerifyResult:
        """解析点位协议地址；ADS 会实际查询 symbol 信息但不读取点值。"""
        async with self._runtime.operation():
            device = self._device(device_id)
            point = device.point(point_id)
            resolved, error = await self._resolve_one(device, point)
            return self._point_result(device, point, resolved, readable=None, error=error)

    async def verify_point(
        self,
        device_id: str,
        point_id: str,
    ) -> PointVerifyResult:
        """实际读取单点并返回 raw/engineering value 与地址信息。"""
        async with self._runtime.operation():
            device = self._device(device_id)
            point = device.point(point_id)
            rows = await self._verify_points_read(device, [point])
            return rows[0]

    async def verify_points(
        self,
        device_id: str,
        *,
        point_group: str | None = None,
    ) -> PointsVerifyResult:
        """批量验证整个设备点表或指定 point_group。"""
        async with self._runtime.operation():
            device = self._device(device_id)
            points = (
                device.point_group_points(point_group)
                if point_group is not None
                else list(device.point_table.points.values())
            )
            if point_group is not None and not points:
                raise CommandError(f"unknown or empty point group '{device_id}/{point_group}'")
            rows = await self._verify_points_read(device, points)
            passed = sum(1 for row in rows if row.ok)
            return PointsVerifyResult(
                device_id=device_id,
                point_group=point_group,
                checked=len(rows),
                passed=passed,
                failed=len(rows) - passed,
                ok=passed == len(rows),
                points=tuple(rows),
            )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _device(self, device_id: str) -> DeviceSession:
        try:
            return self._runtime.device(device_id)
        except KeyError as exc:
            raise CommandError(f"unknown device '{device_id}'") from exc

    async def _resolve_one(
        self,
        device: DeviceSession,
        point: Point,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """解析单点地址；返回 (resolved_address, error)。"""
        if device.protocol_name != "ads":
            return _configured_address(point), None
        probe = self._ads_probe(device)
        if probe is None:
            return None, "ADS symbol probe is not available"
        try:
            await probe.connect()
            resolved = await probe.resolve(point)
        except Exception as exc:
            return None, str(exc) or type(exc).__name__
        finally:
            await probe.close()
        return _resolved_dump(resolved), None

    async def _verify_points_read(
        self,
        device: DeviceSession,
        points: list[Point],
    ) -> list[PointVerifyResult]:
        """逐点解析地址并实际读取；单点失败不终止整批。"""
        probe: SymbolProbe | None = None
        if device.protocol_name == "ads":
            probe = self._ads_probe(device)
            if probe is not None:
                try:
                    await probe.connect()
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    await probe.close()
                    return [
                        self._point_result(device, point, None, readable=False, error=error)
                        for point in points
                    ]
        try:
            resolved_map: dict[str, dict[str, Any] | None] = {}
            errors: dict[str, str] = {}
            for point in points:
                if probe is not None:
                    try:
                        resolved = await probe.resolve(point)
                    except Exception as exc:
                        errors[point.point_id] = str(exc) or type(exc).__name__
                        resolved_map[point.point_id] = None
                        continue
                    resolved_map[point.point_id] = _resolved_dump(resolved)
                else:
                    resolved_map[point.point_id] = _configured_address(point)

            if not await self._runtime.ensure_connected(str(device.device_id)):
                return [
                    self._point_result(
                        device,
                        point,
                        resolved_map.get(point.point_id),
                        readable=False,
                        error=errors.get(point.point_id, "device protocol session is unavailable"),
                    )
                    for point in points
                ]

            rows: list[PointVerifyResult] = []
            readable_points = [point for point in points if point.point_id not in errors]
            for point in points:
                if point.point_id in errors:
                    rows.append(
                        self._point_result(
                            device,
                            point,
                            resolved_map.get(point.point_id),
                            readable=False,
                            error=errors[point.point_id],
                        )
                    )
            if readable_points:
                try:
                    readings = {
                        reading.point_id: reading
                        for reading in await device.read_points(
                            [point.point_id for point in readable_points]
                        )
                    }
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    readings = {}
                    for point in readable_points:
                        rows.append(
                            self._point_result(
                                device,
                                point,
                                resolved_map.get(point.point_id),
                                readable=False,
                                error=error,
                            )
                        )
                    return rows
                for point in readable_points:
                    reading = readings.get(point.point_id)
                    if reading is None:
                        rows.append(
                            self._point_result(
                                device,
                                point,
                                resolved_map.get(point.point_id),
                                readable=False,
                                error="protocol returned no value",
                            )
                        )
                        continue
                    # 诊断同时展示协议原始值：工程值按 scale/offset 逆推。
                    raw_value: Any = reading.value
                    if (
                        isinstance(reading.value, int | float)
                        and not isinstance(reading.value, bool)
                        and (point.scale != 1.0 or point.offset != 0.0)
                    ):
                        raw_value = (reading.value - point.offset) / point.scale
                    readable = reading.quality is not Quality.BAD and reading.value is not None
                    rows.append(
                        self._point_result(
                            device,
                            point,
                            resolved_map.get(point.point_id),
                            readable=readable,
                            raw_value=raw_value,
                            quality=reading.quality.value,
                            source=reading.source,
                            error=(None if readable else "point quality is bad or value is null"),
                        )
                    )
            return rows
        finally:
            if probe is not None:
                await probe.close()

    def _ads_probe(self, device: DeviceSession) -> SymbolProbe | None:
        """为 ADS 设备创建地址解析 probe；options 来自本次操作固定的 generation。"""
        if self._probe_factory is None:
            return None
        generation_config = self._runtime.operation_config()
        return self._probe_factory(
            device.device,
            device.device.endpoint,
            generation_config.device_options_for(device.device_id),
        )

    def _point_result(
        self,
        device: DeviceSession,
        point: Point,
        resolved_address: dict[str, Any] | None,
        *,
        readable: bool | None,
        raw_value: Any = None,
        quality: str | None = None,
        source: str | None = None,
        error: str | None = None,
    ) -> PointVerifyResult:
        meta = device.point_meta(point.point_id)
        engineering = engineering_value(point, raw_value)

        code = DiagnosticCode.OK
        severity = DiagnosticSeverity.INFO
        ok = error is None and (readable is None or readable)
        if error is not None:
            code = (
                DiagnosticCode.POINT_RESOLVE_FAILED
                if resolved_address is None
                else DiagnosticCode.POINT_READ_FAILED
            )
            severity = DiagnosticSeverity.ERROR
            ok = False
        elif readable is False:
            code = DiagnosticCode.POINT_READ_FAILED
            severity = DiagnosticSeverity.ERROR
            ok = False
        elif _ads_mapping_mismatch(point, resolved_address):
            code = DiagnosticCode.POINT_MAPPING_MISMATCH
            severity = DiagnosticSeverity.WARNING
            ok = False
            error = "configured ADS index address differs from PLC symbol resolution"

        return PointVerifyResult(
            device_id=str(device.device_id),
            point_id=point.point_id,
            variable_name=meta.variable_name,
            protocol=device.protocol_name,
            configured_address=_configured_address(point),
            resolved_address=resolved_address,
            data_type=_point_data_type(point),
            scale=point.scale,
            offset=point.offset,
            unit=point.source_unit.code.value,
            readable=readable,
            ok=ok,
            code=code,
            severity=severity,
            raw_value=raw_value,
            engineering_value=engineering,
            quality=quality,
            source=source,
            error=error,
        )


def _point_data_type(point: Point) -> str:
    """点位协议数据类型（ext data_type/type，诊断展示用）。"""
    raw = point.ext.get("data_type", point.ext.get("type"))
    return str(raw) if raw is not None else ""


def _configured_address(point: Point) -> dict[str, Any]:
    """点位配置地址（ext 全部字段，诊断展示用）。"""
    return {key: value for key, value in point.ext.items() if value is not None}


def _resolved_dump(resolved: ResolvedAddress) -> dict[str, Any]:
    return {
        "symbol": resolved.symbol,
        "index_group": resolved.index_group,
        "index_offset": resolved.index_offset,
        "size": resolved.size,
        "protocol_type": resolved.protocol_type,
    }


def _ads_mapping_mismatch(
    point: Point,
    resolved_address: dict[str, Any] | None,
) -> bool:
    """判断 ADS 配置 index 地址是否与 symbol 实际解析地址不一致。"""
    if resolved_address is None:
        return False
    configured_group = point.ext.get("index_group")
    configured_offset = point.ext.get("index_offset")
    if configured_group is None or configured_offset is None:
        return False
    resolved_group = resolved_address.get("index_group")
    resolved_offset = resolved_address.get("index_offset")
    return (
        resolved_group is not None
        and resolved_offset is not None
        and (
            int(configured_group) != int(resolved_group)
            or int(configured_offset) != int(resolved_offset)
        )
    )
