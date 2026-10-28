"""只读配置诊断：复用 Core 已有的 YAML/领域配置校验与快照比较。"""

from __future__ import annotations

from pathlib import Path

from core.application.config_diff import ConfigSnapshotDiff, diff_config_snapshots
from core.application.config_snapshot import ConfigSnapshot
from core.application.diagnostic import (
    DiagnosticFinding,
    DiagnosticResult,
    DiagnosticStatus,
)
from core.infrastructure.config.adapter import YamlConfigAdapter

_REQUIRED_FILES = (
    "system.yaml",
    "units.yaml",
    "device_models.yaml",
    "devices.yaml",
    "points.yaml",
    "tasks.yaml",
    "sinks.yaml",
    "business_points.yaml",
)


def _load(config_dir: Path) -> ConfigSnapshot:
    return YamlConfigAdapter(config_dir).load()


def check_config(config_dir: str | Path) -> DiagnosticResult:
    """执行完整配置检查；不修改文件、不访问设备及 Sink。"""
    base = Path(config_dir)
    if not base.is_dir():
        return _failed(base, "CONFIG_DIRECTORY_NOT_FOUND", "Configuration directory not found")
    missing = [name for name in _REQUIRED_FILES if not (base / name).is_file()]
    if missing:
        return DiagnosticResult(
            category="config",
            target=str(base),
            status=DiagnosticStatus.FAIL,
            findings=tuple(
                DiagnosticFinding(
                    code="CONFIG_FILE_MISSING",
                    status=DiagnosticStatus.FAIL,
                    message="Required configuration file is missing",
                    location=str(base / name),
                )
                for name in missing
            ),
        )
    try:
        snapshot = _load(base)
    except Exception as exc:
        # 不将原始异常消息直接写入报告；校验器可能包含配置中的敏感值。
        return _failed(
            base,
            "CONFIG_INVALID",
            f"Configuration validation failed ({type(exc).__name__})",
        )
    return DiagnosticResult(
        category="config",
        target=str(base),
        status=DiagnosticStatus.PASS,
        findings=(
            DiagnosticFinding(
                code="CONFIG_VALID",
                status=DiagnosticStatus.PASS,
                message="YAML, schema and cross-reference validation passed",
            ),
        ),
        measurements={
            "devices": len(snapshot.site.devices),
            "device_models": len(snapshot.device_models),
            "point_tables": len(snapshot.point_tables),
            "tasks": len(snapshot.tasks),
            "sinks": len(snapshot.sinks),
        },
    )


def compare_config(previous: str | Path, current: str | Path) -> DiagnosticResult:
    """检查两个配置目录并返回快照差异；任一目录无效即拒绝比较。"""
    previous_result = check_config(previous)
    current_result = check_config(current)
    errors = (
        *(
            finding
            for finding in previous_result.findings
            if finding.status is DiagnosticStatus.FAIL
        ),
        *(
            finding
            for finding in current_result.findings
            if finding.status is DiagnosticStatus.FAIL
        ),
    )
    if errors:
        return DiagnosticResult(
            category="config",
            target=f"{previous} -> {current}",
            status=DiagnosticStatus.FAIL,
            findings=tuple(errors),
        )
    try:
        diff = diff_config_snapshots(_load(Path(previous)), _load(Path(current)))
    except Exception as exc:
        return _failed(
            Path(current),
            "CONFIG_DIFF_FAILED",
            f"Configuration comparison failed ({type(exc).__name__})",
        )
    return _diff_result(previous, current, diff)


def _diff_result(
    previous: str | Path,
    current: str | Path,
    diff: ConfigSnapshotDiff,
) -> DiagnosticResult:
    sections = {
        name: {
            "added": sorted(section.added),
            "removed": sorted(section.removed),
            "changed": sorted(section.changed),
        }
        for name, section in diff.sections.items()
    }
    return DiagnosticResult(
        category="config",
        target=f"{previous} -> {current}",
        status=DiagnosticStatus.PASS,
        findings=(
            DiagnosticFinding(
                code="CONFIG_CHANGED" if diff.has_changes else "CONFIG_UNCHANGED",
                status=DiagnosticStatus.PASS,
                message="Configuration differences computed successfully",
            ),
        ),
        evidence={
            "has_changes": diff.has_changes,
            "system_changed": diff.system_changed,
            "site_changed": diff.site_changed,
            "units_changed": diff.units_changed,
            "sections": sections,
        },
    )


def _failed(path: Path, code: str, message: str) -> DiagnosticResult:
    return DiagnosticResult(
        category="config",
        target=str(path),
        status=DiagnosticStatus.FAIL,
        findings=(
            DiagnosticFinding(
                code=code,
                status=DiagnosticStatus.FAIL,
                message=message,
                location=str(path),
            ),
        ),
    )
