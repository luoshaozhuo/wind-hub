"""Server 配置主动验证与 ADS 地址修复。

Server 只负责配置语义、验证编排和安全写回；所有真实 PLC 网络/协议/点读取与
ADS 地址解析统一经 Commander RPC 执行，Server 不创建协议 Driver 或 Probe。
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from wind_hub_core.config.schema import Config, DeviceConfig, PointConfig
from wind_hub_core.validation.models import (
    AddressResolution,
    DeviceValidationReport,
    PointValidationResult,
    ValidationCode,
    ValidationSeverity,
)
from wind_hub_server.application.port.worker import CommanderPort

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    """一次主动验证的汇总。"""

    reports: tuple[DeviceValidationReport, ...]
    repaired_points: int = 0

    @property
    def error_count(self) -> int:
        return sum(
            1
            for report in self.reports
            if report.severity is ValidationSeverity.ERROR
        )


class ServerConfigValidator:
    """通过 Commander 执行现场验证；Server 自身不连接 PLC。"""

    def __init__(
        self,
        config_dir: str | Path,
        commander: CommanderPort,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._commander = commander

    async def validate_startup(self, config: Config) -> ValidationSummary:
        """验证全部已启用设备，并在满足安全条件时修复 ADS 地址。"""
        return await self._validate(config, device_ids=None, allow_repair=True)

    async def validate_added_devices(
        self,
        config: Config,
        device_ids: set[str],
    ) -> ValidationSummary:
        """Worker reload 成功后验证新增设备；不自动修复共享点表。"""
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
        reports: list[DeviceValidationReport] = []
        repairs: dict[str, dict[str, AddressResolution]] = {}

        grouped: dict[str, list[DeviceConfig]] = {}
        for device in devices:
            grouped.setdefault(device.point_table, []).append(device)

        for table_name, table_devices in grouped.items():
            table = config.point_tables.tables[table_name]
            table_reports: list[DeviceValidationReport] = []
            canonical: dict[str, AddressResolution] | None = None
            representative_id: str | None = None

            for device in table_devices:
                report, resolved = await self._validate_device(
                    device,
                    list(table.points),
                )
                table_reports.append(report)
                if (
                    table.protocol == "ads"
                    and canonical is None
                    and report.protocol_ok
                    and report.points
                    and all(point.readable for point in report.points)
                    and resolved
                ):
                    canonical = resolved
                    representative_id = device.device_id

            if table.protocol == "ads" and canonical is not None:
                mismatch = self._configured_mapping_mismatch(
                    list(table.points),
                    canonical,
                )
                validation_complete = all(
                    report.protocol_ok
                    and report.points
                    and all(point.readable for point in report.points)
                    for report in table_reports
                )
                safe_to_repair = allow_repair and validation_complete

                if mismatch:
                    table_repairs: dict[str, AddressResolution] = {}
                    for point in table.points:
                        resolution = canonical.get(point.point_id)
                        if resolution is None:
                            continue
                        extra = point.address.model_extra or {}
                        if (
                            extra.get("index_group") == resolution.index_group
                            and extra.get("index_offset") == resolution.index_offset
                        ):
                            continue
                        logger.warning(
                            (
                                "ADS point address mismatch table=%s point=%s "
                                "configured=(%s,%s) resolved=(%s,%s) "
                                "representative=%s auto_repair=%s"
                            ),
                            table_name,
                            point.point_id,
                            extra.get("index_group"),
                            extra.get("index_offset"),
                            resolution.index_group,
                            resolution.index_offset,
                            representative_id,
                            safe_to_repair,
                        )
                        if safe_to_repair:
                            table_repairs[point.point_id] = resolution

                    if table_repairs:
                        repairs[table_name] = table_repairs
                    if not safe_to_repair:
                        table_reports = [
                            self._mapping_mismatch(report)
                            for report in table_reports
                        ]

            reports.extend(table_reports)

        repaired_points = (
            self._repair_points_yaml(config, repairs)
            if repairs
            else 0
        )

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

    async def _validate_device(
        self,
        device: DeviceConfig,
        points: list[PointConfig],
    ) -> tuple[DeviceValidationReport, dict[str, AddressResolution]]:
        """通过 Commander 获取设备链路和整表在线验证事实。"""
        try:
            device_result = await self._commander.verify_device(device.device_id)
        except Exception as exc:
            return (
                DeviceValidationReport(
                    device_id=device.device_id,
                    point_table=device.point_table,
                    ping_ok=False,
                    port_ok=False,
                    protocol_ok=False,
                    code=ValidationCode.PROTOCOL_CONNECT_FAILED,
                    severity=ValidationSeverity.ERROR,
                    message=str(exc) or type(exc).__name__,
                ),
                {},
            )

        stages = {
            str(stage.get("name")): stage
            for stage in list(device_result.get("stages") or [])
            if isinstance(stage, dict)
        }
        ping_ok = bool((stages.get("network") or {}).get("ok"))
        port_ok = bool((stages.get("transport") or {}).get("ok"))
        protocol_ok = bool((stages.get("protocol") or {}).get("ok"))

        if not port_ok or not protocol_ok:
            if not port_ok:
                code = ValidationCode.TCP_PORT_UNREACHABLE
            else:
                code = ValidationCode.PROTOCOL_CONNECT_FAILED
            message = str(
                (stages.get("transport") or stages.get("protocol") or {}).get(
                    "message"
                )
                or ""
            )
            return (
                DeviceValidationReport(
                    device_id=device.device_id,
                    point_table=device.point_table,
                    ping_ok=ping_ok,
                    port_ok=port_ok,
                    protocol_ok=protocol_ok,
                    code=code,
                    severity=ValidationSeverity.ERROR,
                    message=message,
                ),
                {},
            )

        try:
            result = await self._commander.verify_points(device.device_id)
        except Exception as exc:
            return (
                DeviceValidationReport(
                    device_id=device.device_id,
                    point_table=device.point_table,
                    ping_ok=ping_ok,
                    port_ok=port_ok,
                    protocol_ok=protocol_ok,
                    code=ValidationCode.POINT_READ_FAILED,
                    severity=ValidationSeverity.ERROR,
                    message=str(exc) or type(exc).__name__,
                ),
                {},
            )

        point_rows = {
            str(row.get("point_id")): row
            for row in list(result.get("points") or [])
            if isinstance(row, dict) and row.get("point_id") is not None
        }
        resolved: dict[str, AddressResolution] = {}
        point_results: list[PointValidationResult] = []

        for point in points:
            row = point_rows.get(point.point_id, {})
            readable = bool(row.get("readable") or row.get("ok"))
            raw_resolved = row.get("resolved_address")
            resolution = self._resolution_from_dict(
                point,
                raw_resolved if isinstance(raw_resolved, dict) else None,
            )
            if resolution is not None:
                resolved[point.point_id] = resolution

            point_results.append(
                PointValidationResult(
                    point_id=point.point_id,
                    readable=readable,
                    code=(
                        ValidationCode.OK
                        if readable
                        else ValidationCode.POINT_READ_FAILED
                    ),
                    severity=(
                        ValidationSeverity.INFO
                        if readable
                        else ValidationSeverity.ERROR
                    ),
                    message=str(row.get("error") or ""),
                    resolved=resolution,
                )
            )

        failed = [row.point_id for row in point_results if not row.readable]
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
            else "ping failed but TCP/protocol/point reads succeeded"
            if not ping_ok
            else ""
        )
        return (
            DeviceValidationReport(
                device_id=device.device_id,
                point_table=device.point_table,
                ping_ok=ping_ok,
                port_ok=port_ok,
                protocol_ok=protocol_ok,
                code=code,
                severity=severity,
                message=message,
                points=tuple(point_results),
            ),
            resolved,
        )

    @staticmethod
    def _resolution_from_dict(
        point: PointConfig,
        raw: dict[str, Any] | None,
    ) -> AddressResolution | None:
        if raw is None:
            return None
        index_group = raw.get("index_group")
        index_offset = raw.get("index_offset")
        if index_group is None and index_offset is None:
            return None
        return AddressResolution(
            point_id=point.point_id,
            symbol=(
                str(raw.get("symbol"))
                if raw.get("symbol") is not None
                else None
            ),
            index_group=(
                int(index_group)
                if index_group is not None
                else None
            ),
            index_offset=(
                int(index_offset)
                if index_offset is not None
                else None
            ),
            size=int(raw["size"]) if raw.get("size") is not None else None,
            protocol_type=(
                str(raw.get("protocol_type"))
                if raw.get("protocol_type") is not None
                else point.data_type
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
