"""Read-only diagnostic checks for shared instance and template configuration."""

from __future__ import annotations

from pathlib import Path

from core.application.diagnostic import (
    DiagnosticFinding,
    DiagnosticResult,
    DiagnosticStatus,
)
from core.infrastructure.config.shared_documents import (
    SharedConfigPaths,
    SharedDeviceDefinition,
    SharedDocumentLoader,
    SharedDocuments,
)

_REQUIRED_FILES = (
    "instances/site.yaml",
    "instances/devices.yaml",
    "templates/units.yaml",
    "templates/business_points.yaml",
)


def _load(config_dir: Path) -> SharedDocuments:
    return SharedDocumentLoader(
        SharedConfigPaths(
            instances=config_dir / "instances",
            templates=config_dir / "templates",
        )
    ).load()


def check_config(config_dir: str | Path) -> DiagnosticResult:
    """Check shared configuration only; service settings are owned by services."""
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
        documents = _load(base)
    except Exception as exc:
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
                message="Shared YAML, schema and template references validated",
            ),
        ),
        measurements={
            "devices": len(documents.devices),
            "device_models": len(
                {item.templates.model.reference for item in documents.devices.values()}
            ),
            "point_tables": len(
                {item.templates.point_table.reference for item in documents.devices.values()}
            ),
        },
    )


def compare_config(previous: str | Path, current: str | Path) -> DiagnosticResult:
    """Compare shared configuration after independently validating both roots."""
    previous_result = check_config(previous)
    current_result = check_config(current)
    errors = tuple(
        finding
        for result in (previous_result, current_result)
        for finding in result.findings
        if finding.status is DiagnosticStatus.FAIL
    )
    if errors:
        return DiagnosticResult(
            category="config",
            target=f"{previous} -> {current}",
            status=DiagnosticStatus.FAIL,
            findings=errors,
        )
    try:
        before = _load(Path(previous))
        after = _load(Path(current))
    except Exception as exc:
        return _failed(
            Path(current),
            "CONFIG_DIFF_FAILED",
            f"Configuration comparison failed ({type(exc).__name__})",
        )
    return _diff_result(previous, current, before, after)


def _template_identity(device: SharedDeviceDefinition) -> tuple[tuple[str, str, str], ...]:
    """Stable template identity independent of checkout location."""
    templates = device.templates
    dependencies = (
        templates.model,
        templates.protocol,
        templates.point_table,
        *templates.inherited_tables,
    )
    return tuple(
        (
            item.reference.template_id,
            item.reference.file_version,
            item.content_hash,
        )
        for item in dependencies
    )


def _diff_result(
    previous: str | Path,
    current: str | Path,
    before: SharedDocuments,
    after: SharedDocuments,
) -> DiagnosticResult:
    previous_devices = before.devices
    current_devices = after.devices
    previous_names = set(previous_devices)
    current_names = set(current_devices)
    common = previous_names & current_names
    changed = sorted(
        name
        for name in common
        if (
            previous_devices[name].name,
            previous_devices[name].endpoint,
            previous_devices[name].protocol_options,
            previous_devices[name].enabled,
            _template_identity(previous_devices[name]),
        )
        != (
            current_devices[name].name,
            current_devices[name].endpoint,
            current_devices[name].protocol_options,
            current_devices[name].enabled,
            _template_identity(current_devices[name]),
        )
    )
    # Template changes also affect the revision even when instance values are unchanged.
    has_changes = before.revision != after.revision
    return DiagnosticResult(
        category="config",
        target=f"{previous} -> {current}",
        status=DiagnosticStatus.PASS,
        findings=(
            DiagnosticFinding(
                code="CONFIG_CHANGED" if has_changes else "CONFIG_UNCHANGED",
                status=DiagnosticStatus.PASS,
                message="Shared configuration differences computed successfully",
            ),
        ),
        evidence={
            "has_changes": has_changes,
            "site_changed": before.site != after.site,
            "units_changed": before.units != after.units,
            "business_points_changed": before.business_points != after.business_points,
            "sections": {
                "devices": {
                    "added": sorted(current_names - previous_names),
                    "removed": sorted(previous_names - current_names),
                    "changed": changed,
                }
            },
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
