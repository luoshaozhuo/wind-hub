"""Server 配置主动验证与 ADS 地址修复。

Server 是配置权威：启动时验证所有实际绑定到 Device 的 Point Table；
reload 时只主动验证新增 Device。Collector 仍负责运行期 connect/reconnect
和兜底 symbol 解析，但不写 YAML。
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

import wind_hub.adapter.outbound.protocol  # noqa: F401
from wind_hub.adapter.outbound.protocol.ads import router as ads_router
from wind_hub.config.schema import Config, DeviceConfig, PointConfig
from wind_hub.domain.model.point import PointRef, Quality
from wind_hub.infra.protocol_registry import protocol_registry
from wind_hub_core.protocol.ads import ADSProbe
from wind_hub_core.validation.models import (
    AddressResolution,
    DeviceProbeTarget,
    DeviceValidationReport,
    PointProbeSpec,
    PointValidationResult,
    ValidationCode,
    ValidationSeverity,
)
from wind_hub_core.validation.network import ping_host, tcp_port_open

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    """一次主动验证的汇总。"""

    reports: tuple[DeviceValidationReport, ...]
    repaired_points: int = 0

    @property
    def error_count(self) -> int:
        """ERROR 级设备结果数量。"""
        return sum(
            1
            for report in self.reports
            if report.severity is ValidationSeverity.ERROR
        )


class ServerConfigValidator:
    """Server 权威配置的主动现场验证器。"""

    def __init__(self, config_dir: str | Path) -> None:
        self._config_dir = Path(config_dir)

    async def validate_startup(self, config: Config) -> ValidationSummary:
        """启动时验证全部已启用 Device，并安全修复一致的 ADS 地址。"""
        return await self._validate(config, device_ids=None, allow_repair=True)

    async def validate_added_devices(
        self,
        config: Config,
        device_ids: set[str],
    ) -> ValidationSummary:
        """reload 时只验证新增 Device；首次启用的 Point Table 可安全修正。"""
        if not device_ids:
            return ValidationSummary(reports=())
        return await self._validate(
            config,
            device_ids=device_ids,
            allow_repair=False,
        )

    async def _validate(
        self,
        config: Config,
        *,
        device_ids: set[str] | None,
        allow_repair: bool,
    ) -> ValidationSummary:
        devices = [
            device
            for device in config.devices.devices
            if device.enabled
            and (device_ids is None or device.device_id in device_ids)
        ]
        if not devices:
            return ValidationSummary(reports=())

        if config.system.ads is not None and any(
            device.protocol == "ads" for device in devices
        ):
            await ads_router.ensure_local_initialized(config.system.ads)

        grouped: dict[str, list[DeviceConfig]] = {}
        for device in devices:
            grouped.setdefault(device.point_table, []).append(device)

        reports: list[DeviceValidationReport] = []
        repairs: dict[str, dict[str, AddressResolution]] = {}

        all_enabled = [device for device in config.devices.devices if device.enabled]
        for table_name, table_devices in grouped.items():
            table = config.point_tables.tables[table_name]
            points = list(table.points)

            table_allow_repair = allow_repair
            if device_ids is not None and not table_allow_repair:
                all_users = [
                    device
                    for device in all_enabled
                    if device.point_table == table_name
                ]
                table_allow_repair = bool(all_users) and all(
                    device.device_id in device_ids
                    for device in all_users
                )

            if table.protocol == "ads":
                table_reports, table_repairs = await self._validate_ads_table(
                    table_name,
                    table_devices,
                    points,
                    allow_repair=table_allow_repair,
                )
                reports.extend(table_reports)
                if table_repairs:
                    repairs[table_name] = table_repairs
            else:
                for device in table_devices:
                    reports.append(
                        await self._validate_generic_device(device, points)
                    )

        repaired_points = 0
        if repairs:
            repaired_points = self._repair_points_yaml(config, repairs)

        for report in reports:
            logger.log(
                logging.ERROR
                if report.severity is ValidationSeverity.ERROR
                else logging.WARNING
                if report.severity is ValidationSeverity.WARNING
                else logging.INFO,
                (
                    "config validation device=%s table=%s code=%s "
                    "ping=%s port=%s protocol=%s message=%s"
                ),
                report.device_id,
                report.point_table,
                report.code,
                report.ping_ok,
                report.port_ok,
                report.protocol_ok,
                report.message,
            )
        return ValidationSummary(
            reports=tuple(reports),
            repaired_points=repaired_points,
        )

    async def _validate_ads_table(
        self,
        table_name: str,
        devices: list[DeviceConfig],
        points: list[PointConfig],
        *,
        allow_repair: bool,
    ) -> tuple[list[DeviceValidationReport], dict[str, AddressResolution]]:
        """验证同一 ADS Point Table。

        symbol -> index 地址只在一台代表设备上解析一次，得到 canonical
        mapping；其余设备直接按该 mapping 做点读取验证，不重复 symbol lookup。
        """
        point_specs = [self._point_spec(point) for point in points]
        reports: list[DeviceValidationReport] = []

        configured = self._configured_resolutions(points)
        canonical: dict[str, AddressResolution] | None = None
        representative_id: str | None = None

        if allow_repair or configured is None:
            for device in devices:
                report, resolved = await self._validate_ads_device(
                    device,
                    point_specs,
                    resolutions=None,
                )
                reports.append(report)
                if resolved is not None:
                    canonical = resolved
                    representative_id = device.device_id
                    break
            if canonical is None:
                return reports, {}
        else:
            canonical = configured

        validated_ids = {report.device_id for report in reports}
        for device in devices:
            if device.device_id in validated_ids:
                continue
            report, _ = await self._validate_ads_device(
                device,
                point_specs,
                resolutions=canonical,
            )
            reports.append(report)

        repairs: dict[str, AddressResolution] = {}
        mismatch = self._configured_mapping_mismatch(points, canonical)
        validation_complete = all(
            report.protocol_ok
            and all(point.readable for point in report.points)
            for report in reports
        )
        safe_to_repair = allow_repair and validation_complete

        if mismatch:
            for point in points:
                resolved = canonical.get(point.point_id)
                if resolved is None:
                    continue
                extra = point.address.model_extra or {}
                if (
                    extra.get("index_group") == resolved.index_group
                    and extra.get("index_offset") == resolved.index_offset
                ):
                    continue
                logger.warning(
                    (
                        "ADS point address mismatch table=%s point=%s "
                        "configured=(%s,%s) resolved=(%s,%s) representative=%s "
                        "auto_repair=%s"
                    ),
                    table_name,
                    point.point_id,
                    extra.get("index_group"),
                    extra.get("index_offset"),
                    resolved.index_group,
                    resolved.index_offset,
                    representative_id,
                    safe_to_repair,
                )
                if safe_to_repair:
                    repairs[point.point_id] = resolved

            if not safe_to_repair:
                reports = [self._mapping_mismatch(report) for report in reports]

        return reports, repairs

    async def _validate_ads_device(
        self,
        device: DeviceConfig,
        points: list[PointProbeSpec],
        *,
        resolutions: dict[str, AddressResolution] | None,
    ) -> tuple[DeviceValidationReport, dict[str, AddressResolution] | None]:
        """分层验证单台 ADS Device。

        resolutions 为 None 时，本设备作为代表设备执行一次 symbol 解析；
        否则直接复用 Point Table canonical mapping，只做实际读取验证。
        """
        ping_ok = await ping_host(device.endpoint.host)
        port_ok = await tcp_port_open(device.endpoint.host, device.endpoint.port)
        if not port_ok:
            return (
                DeviceValidationReport(
                    device_id=device.device_id,
                    point_table=device.point_table,
                    ping_ok=ping_ok,
                    port_ok=False,
                    protocol_ok=False,
                    code=ValidationCode.TCP_PORT_UNREACHABLE,
                    severity=ValidationSeverity.ERROR,
                    message=f"TCP port {device.endpoint.port} unreachable",
                ),
                None,
            )

        target = DeviceProbeTarget(
            device_id=device.device_id,
            host=device.endpoint.host,
            options=dict(device.endpoint.extensions),
        )
        probe = ADSProbe(target)
        try:
            await probe.connect()
        except Exception as exc:
            await probe.close()
            return (
                DeviceValidationReport(
                    device_id=device.device_id,
                    point_table=device.point_table,
                    ping_ok=ping_ok,
                    port_ok=True,
                    protocol_ok=False,
                    code=ValidationCode.PROTOCOL_CONNECT_FAILED,
                    severity=ValidationSeverity.ERROR,
                    message=str(exc),
                ),
                None,
            )

        try:
            actual = resolutions
            if actual is None:
                try:
                    actual = await probe.resolve_points(points)
                except Exception as exc:
                    return (
                        DeviceValidationReport(
                            device_id=device.device_id,
                            point_table=device.point_table,
                            ping_ok=ping_ok,
                            port_ok=True,
                            protocol_ok=True,
                            code=ValidationCode.POINT_RESOLVE_FAILED,
                            severity=ValidationSeverity.ERROR,
                            message=str(exc),
                        ),
                        None,
                    )

            readable = await probe.verify_read(points, actual)
            point_results = tuple(
                PointValidationResult(
                    point_id=point.point_id,
                    readable=point.point_id in readable,
                    code=(
                        ValidationCode.OK
                        if point.point_id in readable
                        else ValidationCode.POINT_READ_FAILED
                    ),
                    severity=(
                        ValidationSeverity.INFO
                        if point.point_id in readable
                        else ValidationSeverity.ERROR
                    ),
                    resolved=actual.get(point.point_id),
                )
                for point in points
            )
            failed = [item.point_id for item in point_results if not item.readable]
            code = (
                ValidationCode.POINT_READ_FAILED
                if failed
                else ValidationCode.PING_FAILED
                if not ping_ok
                else ValidationCode.OK
            )
            severity = (
                ValidationSeverity.ERROR
                if failed
                else ValidationSeverity.WARNING
                if not ping_ok
                else ValidationSeverity.INFO
            )
            message = (
                f"unreadable points: {failed}"
                if failed
                else "ping failed but TCP/ADS/point reads succeeded"
                if not ping_ok
                else ""
            )
            return (
                DeviceValidationReport(
                    device_id=device.device_id,
                    point_table=device.point_table,
                    ping_ok=ping_ok,
                    port_ok=True,
                    protocol_ok=True,
                    code=code,
                    severity=severity,
                    message=message,
                    points=point_results,
                ),
                actual,
            )
        finally:
            await probe.close()

    @staticmethod
    def _configured_resolutions(
        points: list[PointConfig],
    ) -> dict[str, AddressResolution] | None:
        """从 YAML resolved 点表提取完整 canonical index mapping。

        任一点缺 index_group/index_offset 时返回 None，表示必须选代表设备
        做一次 symbol resolution。
        """
        result: dict[str, AddressResolution] = {}
        for point in points:
            extra = point.address.model_extra or {}
            index_group = extra.get("index_group")
            index_offset = extra.get("index_offset")
            if index_group is None or index_offset is None:
                return None
            symbol = extra.get("symbol")
            result[point.point_id] = AddressResolution(
                point_id=point.point_id,
                symbol=str(symbol) if symbol is not None else None,
                index_group=int(index_group),
                index_offset=int(index_offset),
                protocol_type=point.data_type,
            )
        return result

    async def _validate_generic_device(
        self,
        device: DeviceConfig,
        points: list[PointConfig],
    ) -> DeviceValidationReport:
        """验证 Modbus/IEC104 Device 的网络、协议和点可读性。"""
        ping_ok = await ping_host(device.endpoint.host)
        port_ok = await tcp_port_open(device.endpoint.host, device.endpoint.port)
        if not port_ok:
            return DeviceValidationReport(
                device_id=device.device_id,
                point_table=device.point_table,
                ping_ok=ping_ok,
                port_ok=False,
                protocol_ok=False,
                code=ValidationCode.TCP_PORT_UNREACHABLE,
                severity=ValidationSeverity.ERROR,
                message=f"TCP port {device.endpoint.port} unreachable",
            )

        driver = protocol_registry.create(device.protocol, device)
        driver.set_points_mapping(points)
        try:
            await driver.connect()
        except Exception as exc:
            await driver.close()
            return DeviceValidationReport(
                device_id=device.device_id,
                point_table=device.point_table,
                ping_ok=ping_ok,
                port_ok=True,
                protocol_ok=False,
                code=ValidationCode.PROTOCOL_CONNECT_FAILED,
                severity=ValidationSeverity.ERROR,
                message=str(exc),
            )

        try:
            refs = [
                PointRef(device_id=device.device_id, point_id=point.point_id)
                for point in points
            ]
            try:
                values = await driver.read(refs)
            except Exception as exc:
                return DeviceValidationReport(
                    device_id=device.device_id,
                    point_table=device.point_table,
                    ping_ok=ping_ok,
                    port_ok=True,
                    protocol_ok=True,
                    code=ValidationCode.POINT_READ_FAILED,
                    severity=ValidationSeverity.ERROR,
                    message=str(exc),
                    points=tuple(
                        PointValidationResult(
                            point_id=point.point_id,
                            readable=False,
                            code=ValidationCode.POINT_READ_FAILED,
                            severity=ValidationSeverity.ERROR,
                        )
                        for point in points
                    ),
                )

            by_id = {value.point_id: value for value in values}
            point_results = tuple(
                PointValidationResult(
                    point_id=point.point_id,
                    readable=(
                        point.point_id in by_id
                        and by_id[point.point_id].quality == Quality.GOOD
                    ),
                    code=(
                        ValidationCode.OK
                        if point.point_id in by_id
                        and by_id[point.point_id].quality == Quality.GOOD
                        else ValidationCode.POINT_READ_FAILED
                    ),
                    severity=(
                        ValidationSeverity.INFO
                        if point.point_id in by_id
                        and by_id[point.point_id].quality is Quality.GOOD
                        else ValidationSeverity.ERROR
                    ),
                )
                for point in points
            )
            failed = [item.point_id for item in point_results if not item.readable]
            return DeviceValidationReport(
                device_id=device.device_id,
                point_table=device.point_table,
                ping_ok=ping_ok,
                port_ok=True,
                protocol_ok=True,
                code=(
                    ValidationCode.POINT_READ_FAILED
                    if failed
                    else ValidationCode.PING_FAILED
                    if not ping_ok
                    else ValidationCode.OK
                ),
                severity=(
                    ValidationSeverity.ERROR
                    if failed
                    else ValidationSeverity.WARNING
                    if not ping_ok
                    else ValidationSeverity.INFO
                ),
                message=(
                    f"unreadable points: {failed}"
                    if failed
                    else "ping failed but TCP/protocol/point reads succeeded"
                    if not ping_ok
                    else ""
                ),
                points=point_results,
            )
        finally:
            await driver.close()

    @staticmethod
    def _point_spec(point: PointConfig) -> PointProbeSpec:
        """把 Collector 配置模型转换为 common probe 输入。"""
        return PointProbeSpec(
            point_id=point.point_id,
            data_type=point.data_type,
            address=point.address.model_dump(
                mode="python",
                exclude_none=True,
            ),
        )

    @staticmethod
    def _configured_mapping_mismatch(
        points: list[PointConfig],
        actual: dict[str, AddressResolution],
    ) -> bool:
        """新增设备实际 mapping 是否偏离当前共享配置。"""
        for point in points:
            resolved = actual.get(point.point_id)
            if resolved is None:
                continue
            extra = point.address.model_extra or {}
            if (
                extra.get("index_group") != resolved.index_group
                or extra.get("index_offset") != resolved.index_offset
            ):
                return True
        return False

    @staticmethod
    def _mapping_mismatch(
        report: DeviceValidationReport,
    ) -> DeviceValidationReport:
        """把设备报告提升为共享点表 mapping 不一致错误。"""
        return DeviceValidationReport(
            device_id=report.device_id,
            point_table=report.point_table,
            ping_ok=report.ping_ok,
            port_ok=report.port_ok,
            protocol_ok=report.protocol_ok,
            code=ValidationCode.POINT_MAPPING_MISMATCH,
            severity=ValidationSeverity.ERROR,
            message="ADS mapping differs from shared point-table mapping",
            points=report.points,
        )

    def _repair_points_yaml(
        self,
        config: Config,
        repairs: dict[str, dict[str, AddressResolution]],
    ) -> int:
        """备份 points.yaml 后，把 canonical ADS 地址写回具体使用表。

        对继承点不修改父表；而是在当前被使用的表增加/更新 point patch，
        从而避免影响使用同一父表的其他型号。
        """
        path = self._config_dir / "points.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        tables = raw.get("point_tables")
        if not isinstance(tables, dict):
            raise ValueError("points.yaml has no point_tables mapping")

        changed = 0
        for table_name, table_repairs in repairs.items():
            raw_table = tables.get(table_name)
            if not isinstance(raw_table, dict):
                continue
            patches = raw_table.setdefault("points", [])
            if not isinstance(patches, list):
                raise ValueError(f"point table '{table_name}' points must be a list")
            by_id = {
                patch.get("point_id"): patch
                for patch in patches
                if isinstance(patch, dict)
            }
            resolved_table = config.point_tables.tables[table_name]
            resolved_points = {
                point.point_id: point
                for point in resolved_table.points
            }

            for point_id, resolution in table_repairs.items():
                source = resolved_points[point_id]
                address = source.address.model_dump(
                    mode="python",
                    exclude_none=True,
                )
                address["index_group"] = resolution.index_group
                address["index_offset"] = resolution.index_offset
                patch = by_id.get(point_id)
                if patch is None:
                    patch = {"point_id": point_id}
                    patches.append(patch)
                    by_id[point_id] = patch
                if patch.get("address") == address:
                    continue
                patch["address"] = address
                changed += 1

        if changed == 0:
            return 0

        self._backup_points_file(path)
        content = yaml.safe_dump(
            raw,
            allow_unicode=True,
            sort_keys=False,
        )
        temp = path.with_name(f".{path.name}.validation.tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
        logger.warning(
            "active validation repaired %d ADS point addresses in %s",
            changed,
            path,
        )
        return changed

    def _backup_points_file(self, path: Path) -> None:
        """在自动修正前保存原始 points.yaml。"""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        target = (
            self._config_dir
            / ".history"
            / "validation-backups"
            / stamp
            / path.name
        )
        target.parent.mkdir(parents=True, exist_ok=False)
        shutil.copy2(path, target)
