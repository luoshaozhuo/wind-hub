"""Server application 结构约束：usecase 容器已移除，AppContext 指向新对象。"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "wind_hub_server"


def _source_files() -> list[Path]:
    return [
        path
        for path in SRC_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    ]


def test_usecase_package_removed() -> None:
    """application/usecase 平铺容器不复存在。"""
    assert not (SRC_ROOT / "application" / "usecase").exists()


def test_no_production_import_of_application_usecase() -> None:
    """Server 生产代码不再引用 application.usecase。"""
    offenders = [
        path
        for path in _source_files()
        if "application.usecase" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_legacy_usecase_class_names() -> None:
    """Server 生产代码不再残留旧 XXXUseCase 类名。"""
    legacy = (
        "TaskAssignmentUseCase",
        "CollectorTaskUseCase",
        "WorkerRegistryUseCase",
        "CollectorAggregateUseCase",
        "DeviceUseCase",
        "DeviceControlUseCase",
        "DeviceDataUseCase",
        "LogsUseCase",
        "QualityUseCase",
        "OverviewUseCase",
        "SystemHealthUseCase",
        "DefinitionsUseCase",
        "ConfigUseCase",
        "ConfigAdminUseCase",
        "AdminStateUseCase",
        "SettingsUseCase",
        "SinkUseCase",
        "DiagnosticUseCase",
    )
    offenders = [
        (path, name)
        for path in _source_files()
        for name in legacy
        if name in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_app_context_points_at_new_capabilities() -> None:
    """AppContext 字段类型全部来自新 capability 对象，无 UseCase 命名。"""
    from wind_hub_server.application.app_context import AppContext

    fields = {field.name: field.type for field in dataclasses.fields(AppContext)}
    assert fields, "AppContext must declare fields"
    for name, field_type in fields.items():
        assert "UseCase" not in str(field_type), name

    expected = {
        "config": "ConfigService",
        "tasks": "TaskControlService",
        "devices": "DeviceQueryService",
        "device_data": "DeviceDataService",
        "device_control": "DeviceCommandService",
        "overview": "OverviewService",
        "operations": "OperationRegistry",
        "admin_state": "AdminStateService",
        "config_admin": "ConfigFileService",
        "settings": "SettingsService",
        "definitions": "DefinitionQueryService",
        "sinks": "SinkService",
        "diagnostics": "DiagnosticService",
        "quality": "QualityService",
        "logs": "LogQueryService",
        "system_health": "SystemHealthService",
        "workers": "WorkerRegistry",
    }
    for name, class_name in expected.items():
        assert class_name in str(fields[name]), name


def test_assembly_single_owner_per_service() -> None:
    """assembly 中每个 service/registry 只有一个构造点。"""
    source = inspect.getsource(_assembly_module())
    constructed = (
        "TaskPlacementRegistry(",
        "TaskPlacementReconciler(",
        "TaskControlService(",
        "WorkerRegistry(",
        "CollectorStatusAggregator(",
        "DeviceQueryService(",
        "DeviceCommandService(",
        "DeviceDataService(",
        "DiagnosticService(",
        "OverviewService(",
        "QualityService(",
        "SystemHealthService(",
        "LogQueryService(",
        "ConfigService(",
        "ConfigFileService(",
        "AdminStateService(",
        "SettingsService(",
        "DefinitionQueryService(",
        "SinkService(",
        "OperationRegistry(",
        "AppContext(",
    )
    for token in constructed:
        assert source.count(token) == 1, token


def test_assembly_exposes_task_control_plane() -> None:
    """ServerApp 显式暴露 placement / reconciler / control 三个独立对象。"""
    from wind_hub_server.assembly import ServerApp

    fields = {field.name for field in dataclasses.fields(ServerApp)}
    assert {"task_placements", "task_reconciler", "tasks"} <= fields


def _assembly_module():
    from wind_hub_server import assembly

    return assembly
