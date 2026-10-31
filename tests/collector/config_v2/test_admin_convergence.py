"""Admin 收敛编排：受影响设备、失败隔离、重启边界、收敛重试与意图变更。

文件事务、校验、版本与磁盘写入全部是生产实现；CollectorConfigService 的
运行时边界以脚本化 ReloadResult 替身驱动（真实 NATS/PLC 链路在第三/六轮
系统测试覆盖）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest

from collector.application.reload import ConfigDiff, DeviceDiff, ReloadResult
from collector.infrastructure.config.admin import CollectorConfigAdmin
from collector.infrastructure.config.transactions import (
    CollectorConfigTransactions,
    ConfigSelector,
)
from core.infrastructure.config.yaml import read_yaml_mapping
from tests.collector.config_v2.config_tree import ConfigTree  # noqa: E402

pytestmark = pytest.mark.unit

SERVICE = ConfigSelector("SERVICE", "collector")
DEVICES = ConfigSelector("INSTANCE", "devices")
UNITS = ConfigSelector("TEMPLATE", "units")


@dataclass
class ScriptedConfigService:
    outcomes: list[ReloadResult] = field(default_factory=list)
    desired_states: dict[str, str] = field(default_factory=dict)
    reload_calls: int = 0

    @property
    def current_config(self) -> SimpleNamespace:
        return SimpleNamespace(desired_states=dict(self.desired_states))

    async def reload(self) -> ReloadResult:
        index = min(self.reload_calls, len(self.outcomes) - 1)
        self.reload_calls += 1
        return self.outcomes[index]


def _admin(config_tree, documents, service: ScriptedConfigService) -> CollectorConfigAdmin:
    store = CollectorConfigTransactions(config_tree.paths, documents)
    return CollectorConfigAdmin(store, service, authorize=lambda selector, operation: None)


def _outcome(
    *,
    success: bool,
    added=(),
    updated=(),
    removed=(),
    errors=(),
) -> ReloadResult:
    return ReloadResult(
        success=success,
        diff=ConfigDiff(
            devices=DeviceDiff(added=list(added), updated=list(updated), removed=list(removed))
        ),
        errors=list(errors),
    )


async def test_service_mutation_never_invokes_runtime_reload(
    config_tree: ConfigTree, documents
) -> None:
    service = ScriptedConfigService([_outcome(success=True)])
    admin = _admin(config_tree, documents, service)
    doc, revision = admin._store.read(SERVICE)  # noqa: SLF001
    doc = dict(doc)
    doc["watchdog"] = dict(doc["watchdog"], interval=7.0)
    result = await admin.replace(SERVICE, doc, expected_revision=revision, request_id="r1")
    assert result.status == "RESTART_REQUIRED"
    assert service.reload_calls == 0


async def test_successful_device_change_marks_all_affected_applied(
    config_tree: ConfigTree, documents
) -> None:
    service = ScriptedConfigService([_outcome(success=True, updated=["WT01"])])
    admin = _admin(config_tree, documents, service)
    devices, revision = admin._store.read(DEVICES)  # noqa: SLF001
    devices = dict(devices)
    devices["devices"]["WT01"]["endpoint"]["port"] = 1503
    result = await admin.replace(DEVICES, devices, expected_revision=revision, request_id="r1")
    assert result.status == "APPLIED"
    assert result.devices[0].device_id == "WT01"
    assert result.devices[0].applied
    assert service.reload_calls == 1


async def test_failure_isolation_marks_only_failing_device(
    config_tree: ConfigTree, documents
) -> None:
    service = ScriptedConfigService(
        [
            _outcome(
                success=False,
                updated=["WT01"],
                added=["WT02"],
                errors=["device:WT01: connect refused"],
            )
        ]
    )
    admin = _admin(config_tree, documents, service)
    devices, revision = admin._store.read(DEVICES)  # noqa: SLF001
    devices = dict(devices)
    devices["devices"]["WT02"] = dict(devices["devices"]["WT01"])
    result = await admin.replace(DEVICES, devices, expected_revision=revision, request_id="r1")
    assert result.status == "APPLY_FAILED"
    by_device = {item.device_id: item for item in result.devices}
    assert by_device["WT01"].applied is False
    assert "connect refused" in by_device["WT01"].error
    assert by_device["WT02"].applied is True
    # 非设备级错误必须波及全部受影响设备（fail-closed）。
    service.outcomes.append(
        _outcome(success=False, updated=["WT01"], errors=["messaging unavailable"])
    )
    _, revision2 = admin._store.read(DEVICES)  # noqa: SLF001
    devices = read_yaml_mapping(config_tree.devices_yaml)
    devices["devices"]["WT01"]["endpoint"]["port"] = 1505
    result2 = await admin.replace(DEVICES, devices, expected_revision=revision2, request_id="r2")
    assert all(not item.applied for item in result2.devices)


async def test_retry_converges_without_rewriting_file(config_tree: ConfigTree, documents) -> None:
    service = ScriptedConfigService(
        [
            _outcome(success=False, updated=["WT01"], errors=["device:WT01: boot"]),
            _outcome(success=True, updated=["WT01"]),
        ]
    )
    admin = _admin(config_tree, documents, service)
    devices, revision = admin._store.read(DEVICES)  # noqa: SLF001
    devices = dict(devices)
    devices["devices"]["WT01"]["endpoint"]["port"] = 1503
    first = await admin.replace(DEVICES, devices, expected_revision=revision, request_id="r1")
    assert first.status == "APPLY_FAILED"
    mtime = config_tree.devices_yaml.stat().st_mtime_ns
    await admin.retry_failed()
    assert config_tree.devices_yaml.stat().st_mtime_ns == mtime  # 重试不重写
    status = admin.status(DEVICES)
    assert status.status == "APPLIED"
    assert status.active_revision == first.stored_revision
    assert service.reload_calls == 2


async def test_pure_desired_state_change_is_affected_device(
    config_tree: ConfigTree, documents
) -> None:
    service = ScriptedConfigService([_outcome(success=True)])
    service.desired_states = {"WT01": "STOP"}
    admin = _admin(config_tree, documents, service)
    devices, revision = admin._store.read(DEVICES)  # noqa: SLF001
    devices = dict(devices)
    devices["devices"]["WT01"].setdefault("overrides", {}).setdefault("task", {})
    devices["devices"]["WT01"]["overrides"]["task"]["desired_state"] = "START"
    result = await admin.replace(DEVICES, devices, expected_revision=revision, request_id="r1")
    assert result.status == "APPLIED"
    assert [item.device_id for item in result.devices] == ["WT01"]


async def test_set_task_desired_persists_and_converges(config_tree: ConfigTree, documents) -> None:
    service = ScriptedConfigService([_outcome(success=True, updated=["WT01"])])
    admin = _admin(config_tree, documents, service)
    _, revision = admin._store.read(DEVICES)  # noqa: SLF001
    result = await admin.set_task_desired(
        "WT01", "START", expected_revision=revision, request_id="t1"
    )
    assert result.status == "APPLIED"
    persisted = read_yaml_mapping(config_tree.devices_yaml)
    assert persisted["devices"]["WT01"]["overrides"]["task"]["desired_state"] == "START"


async def test_set_task_desired_conflict_and_unknown_device(
    config_tree: ConfigTree, documents
) -> None:
    service = ScriptedConfigService([_outcome(success=True)])
    admin = _admin(config_tree, documents, service)
    _, revision = admin._store.read(DEVICES)  # noqa: SLF001
    with pytest.raises(KeyError):
        await admin.set_task_desired("NOPE", "START", expected_revision=revision, request_id="t1")
    with pytest.raises(Exception, match="revision conflict"):
        await admin.set_task_desired("WT01", "START", expected_revision="stale", request_id="t2")


async def test_units_catalog_replace_is_restart_only(config_tree: ConfigTree, documents) -> None:
    service = ScriptedConfigService([_outcome(success=True)])
    admin = _admin(config_tree, documents, service)
    units, revision = admin._store.read(UNITS)  # noqa: SLF001
    units = dict(units)
    units["units"]["extra"] = {"symbol": "E", "quantity": "dimensionless"}
    result = await admin.replace(UNITS, units, expected_revision=revision, request_id="u1")
    assert result.status == "RESTART_REQUIRED"
    assert service.reload_calls == 0
    assert admin.runtime_status == "RESTART_REQUIRED"


async def test_template_publication_authorization(config_tree: ConfigTree, documents) -> None:
    from core.application.errors import ConfigError as _ConfigError  # noqa: F401

    service = ScriptedConfigService([_outcome(success=True)])

    def authorize(selector: ConfigSelector, operation: str) -> None:
        if selector.scope == "TEMPLATE" and operation == "write":
            raise PermissionError("template publication is not authorized")

    admin = _admin(config_tree, documents, service)
    admin._authorize = authorize  # noqa: SLF001
    ref = ConfigSelector("TEMPLATE", "protocols", "modbus_tcp", "2.0.0")
    doc, _ = admin._store.read(  # noqa: SLF001
        ConfigSelector("TEMPLATE", "protocols", "modbus_tcp", "1.0.0")
    )
    doc = dict(doc)
    doc["template_version"] = "2.0.0"
    with pytest.raises(PermissionError):
        await admin.replace(ref, doc, expected_revision="CREATE", request_id="pub1")
