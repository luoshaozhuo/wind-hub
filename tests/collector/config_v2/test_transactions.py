"""乐观版本、幂等、不可变模板、原子性、凭据脱敏与并发写。"""

from __future__ import annotations

import asyncio

import pytest

from collector.infrastructure.config.transactions import (
    CollectorConfigTransactions,
    ConfigSelector,
    PublishedTemplateExistsError,
    RevisionConflictError,
)
from core.application.errors import ConfigError
from core.infrastructure.config.versioned_templates import config_hash
from core.infrastructure.config.yaml import read_yaml_mapping
from tests.collector.config_v2.config_tree import ConfigTree  # noqa: E402

pytestmark = pytest.mark.unit

SERVICE = ConfigSelector("SERVICE", "collector")
DEVICES = ConfigSelector("INSTANCE", "devices")
SITE = ConfigSelector("INSTANCE", "site")
UNITS = ConfigSelector("TEMPLATE", "units")


def _store(config_tree, documents) -> CollectorConfigTransactions:
    return CollectorConfigTransactions(config_tree.paths, documents)


async def test_baseline_status_applied_and_revision_matches_file(
    config_tree: ConfigTree, documents
) -> None:
    store = _store(config_tree, documents)
    raw, revision = store.read(DEVICES)
    status = store.status(DEVICES)
    assert status.status == "APPLIED"
    assert status.stored_revision == status.active_revision == revision
    assert revision == config_hash(raw)


async def test_service_replace_marks_restart_required_without_activation(
    config_tree: ConfigTree, documents
) -> None:
    store = _store(config_tree, documents)
    service = config_tree.read("services/collector.yaml")
    _, expected = store.read(SERVICE)
    service["watchdog"]["health_timeout"] = 2.5
    result = await store.replace(SERVICE, service, expected_revision=expected, request_id="req-1")
    assert result.status == "RESTART_REQUIRED"
    assert result.active_revision == expected  # 存储版本前移，生效版本不动
    assert store.status(SERVICE).status == "RESTART_REQUIRED"


async def test_devices_replace_waits_for_separate_apply(config_tree: ConfigTree, documents) -> None:
    store = _store(config_tree, documents)
    devices = config_tree.read("instances/devices.yaml")
    _, expected = store.read(DEVICES)
    devices["devices"]["WT01"]["endpoint"]["port"] = 1503
    result = await store.replace(DEVICES, devices, expected_revision=expected, request_id="req-1")
    assert result.status == "STORED"
    assert result.stored_revision != expected
    assert result.active_revision == expected
    # mark_applied 是独立的收敛动作。
    applied = store.mark_applied(DEVICES, result.stored_revision)
    assert applied.status == "APPLIED"
    assert applied.active_revision == result.stored_revision


async def test_optimistic_conflict_rejected(config_tree: ConfigTree, documents) -> None:
    store = _store(config_tree, documents)
    devices = config_tree.read("instances/devices.yaml")
    with pytest.raises(RevisionConflictError):
        await store.replace(DEVICES, devices, expected_revision="stale", request_id="req-1")


async def test_create_requires_create_revision(config_tree: ConfigTree, documents) -> None:
    store = _store(config_tree, documents)
    ref = ConfigSelector("TEMPLATE", "protocols", "modbus_tcp", "2.0.0")
    doc = config_tree.read("templates/protocols/modbus_tcp/1.0.0.yaml")
    doc["template_version"] = "2.0.0"
    with pytest.raises(ConfigError, match="CREATE"):
        await store.replace(ref, doc, expected_revision="0", request_id="req-x")
    created = await store.replace(ref, doc, expected_revision="CREATE", request_id="req-new")
    assert created.status == "STORED"
    assert (config_tree.templates / "protocols/modbus_tcp/2.0.0.yaml").is_file()


async def test_published_template_is_immutable(config_tree: ConfigTree, documents) -> None:
    store = _store(config_tree, documents)
    ref = ConfigSelector("TEMPLATE", "protocols", "modbus_tcp", "1.0.0")
    doc, revision = store.read(ref)
    doc = dict(doc)
    with pytest.raises(PublishedTemplateExistsError):
        await store.replace(ref, doc, expected_revision=revision, request_id="req-1")


async def test_units_and_business_points_are_mutable(config_tree: ConfigTree, documents) -> None:
    store = _store(config_tree, documents)
    units, revision = store.read(UNITS)
    units = dict(units)
    units["units"]["new_unit"] = {
        "symbol": "NU",
        "quantity": "dimensionless",
    }
    result = await store.replace(UNITS, units, expected_revision=revision, request_id="req-u")
    assert result.status == "RESTART_REQUIRED"  # 单位目录只能重启生效


async def test_idempotent_replay_returns_same_result_without_rewrite(
    config_tree: ConfigTree, documents
) -> None:
    store = _store(config_tree, documents)
    devices, revision = store.read(DEVICES)
    devices = dict(devices)
    devices["devices"]["WT01"]["endpoint"]["port"] = 1504
    first = await store.replace(DEVICES, devices, expected_revision=revision, request_id="idem-1")
    path = config_tree.devices_yaml
    mtime_before = path.stat().st_mtime_ns
    second = await store.replace(DEVICES, devices, expected_revision=revision, request_id="idem-1")
    assert second.stored_revision == first.stored_revision
    assert path.stat().st_mtime_ns == mtime_before
    # 同一 request_id 携带不同载荷必须被拒绝。
    different = dict(devices)
    different["devices"]["WT01"]["endpoint"]["port"] = 1505
    with pytest.raises(ConfigError, match="request_id reused"):
        await store.replace(DEVICES, different, expected_revision=revision, request_id="idem-1")


async def test_invalid_candidate_does_not_touch_file(config_tree: ConfigTree, documents) -> None:
    store = _store(config_tree, documents)
    devices, revision = store.read(DEVICES)
    path = config_tree.devices_yaml
    raw_before = path.read_bytes()
    bad = dict(devices)
    bad["devices"] = []  # devices 必须是 mapping
    with pytest.raises(ConfigError):
        await store.replace(DEVICES, bad, expected_revision=revision, request_id="req-bad")
    assert path.read_bytes() == raw_before
    # 失败不留状态缓存：基线仍按磁盘内容报告 APPLIED。
    assert store.status(DEVICES).status == "APPLIED"


async def test_update_patches_existing_fields_only(config_tree: ConfigTree, documents) -> None:
    store = _store(config_tree, documents)
    _, revision = store.read(DEVICES)
    from collector.infrastructure.config.config_patches import ConfigPatch

    result = await store.update(
        DEVICES,
        (ConfigPatch("/devices/WT01/endpoint/port", "SET", 1506),),
        expected_revision=revision,
        request_id="patch-1",
    )
    assert (
        read_yaml_mapping(config_tree.devices_yaml)["devices"]["WT01"]["endpoint"]["port"] == 1506
    )
    assert result.status == "STORED"
    # 未知字段 SET 必须失败；多补丁中任一失败全部不生效。
    _, revision2 = store.read(DEVICES)
    bad = (
        ConfigPatch("/devices/WT01/endpoint/port", "SET", 1507),
        ConfigPatch("/devices/WT01/new_field", "SET", 1),
    )
    with pytest.raises(ConfigError, match="unknown patch field"):
        await store.update(DEVICES, bad, expected_revision=revision2, request_id="patch-bad")
    assert (
        read_yaml_mapping(config_tree.devices_yaml)["devices"]["WT01"]["endpoint"]["port"] == 1506
    )
    # REMOVE 受 schema 拒绝（endpoint.port 必填语义由装配校验）
    with pytest.raises(ConfigError):
        await store.update(
            DEVICES,
            (ConfigPatch("/devices/WT01/endpoint/host", "REMOVE"),),
            expected_revision=revision2,
            request_id="patch-remove",
        )


async def test_concurrent_replacers_only_one_wins(config_tree: ConfigTree, documents) -> None:
    store = _store(config_tree, documents)
    _, revision = store.read(SERVICE)
    base = config_tree.read("services/collector.yaml")

    async def replace(health_timeout: float, request_id: str):
        candidate = dict(base)
        candidate["watchdog"] = dict(candidate["watchdog"], health_timeout=health_timeout)
        return await store.replace(
            SERVICE,
            candidate,
            expected_revision=revision,
            request_id=request_id,
        )

    results = await asyncio.gather(
        *(replace(1.0 + i, f"req-{i}") for i in range(6)),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, RevisionConflictError)]
    assert len(successes) == 1
    assert len(conflicts) == 5


async def test_redaction_hides_compound_credential_fields(
    config_tree: ConfigTree, documents
) -> None:
    store = _store(config_tree, documents)
    ref = ConfigSelector("TEMPLATE", "protocols", "modbus_tcp", "1.0.0")
    raw, revision = store.read(ref)
    # 协议模板允许承载任意连接选项；含密钥的嵌套字段必须脱敏。
    raw = dict(raw)
    raw["options"] = {"password": "secret", "nested": {"api_key": "k", "ok": 1}}
    from collector.infrastructure.config.transactions import redact_config

    masked = redact_config(raw)
    assert masked["options"]["password"] == "***"
    assert masked["options"]["nested"]["api_key"] == "***"
    assert masked["options"]["nested"]["ok"] == 1
    # read_redacted 返回真实文件内容的脱敏版本（revision 仍是真值）。
    seen, seen_revision = store.read_redacted(ref)
    assert seen_revision == revision


async def test_unsupported_selector_rejected(config_tree: ConfigTree, documents) -> None:
    store = _store(config_tree, documents)
    with pytest.raises(ConfigError, match="unsupported"):
        store.read(ConfigSelector("INSTANCE", "nope"))
    with pytest.raises(ConfigError, match="unsupported"):
        store.read(ConfigSelector("SERVICE", "other"))


async def test_mark_applied_rejects_unknown_or_superseded_revision(
    config_tree: ConfigTree, documents
) -> None:
    store = _store(config_tree, documents)
    with pytest.raises(ConfigError, match="unknown or superseded"):
        store.mark_applied(DEVICES, "deadbeef")
