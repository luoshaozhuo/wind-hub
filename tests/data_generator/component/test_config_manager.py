"""UC-04 组件级：候选/磁盘/运行时快照、冲突、不可变版本与提交恢复。

直接驱动真实文件树与 SnapshotManager（无 mock），Core 共享文件来自仓库真实
模板；事务日志、fsync、os.replace 均为生产代码路径。
"""

from __future__ import annotations

import copy
import json

import pytest
import yaml

from data_generator.domain.model import GeneratorError
from data_generator.infrastructure.config import load_settings
from data_generator.infrastructure.snapshots import (
    FileContent,
    SnapshotManager,
    recover_transaction,
)

from ..conftest import ship_scenarios_document

pytestmark = [pytest.mark.component, pytest.mark.reliability, pytest.mark.conflict]


def _manager(service_path) -> tuple[SnapshotManager, dict[str, FileContent]]:  # noqa: ANN001
    recover_transaction(service_path.absolute().parent.parent)
    settings = load_settings(service_path)
    manager = SnapshotManager(settings)
    disk = {item.path: item for item in manager.get("DISK")}
    return manager, disk


def test_three_scopes_and_unknown_candidate(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    manager, disk = _manager(service_path)
    assert manager.get("RUNTIME") == manager.get("DISK")
    assert any(path.startswith("instances/site.yaml") for path in disk)
    assert any(path.startswith("templates/point_tables/") for path in disk)
    with pytest.raises(GeneratorError, match="candidate not found"):
        manager.get("CANDIDATE", "nope")
    with pytest.raises(GeneratorError):
        manager.get("ELSEWHERE")


def test_push_requires_expected_revision_and_full_set(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    manager, disk = _manager(service_path)
    files = tuple(disk.values())
    with pytest.raises(GeneratorError, match="expected_revision"):
        manager.push(files, "wrong")
    with pytest.raises(GeneratorError, match="complete current file set"):
        manager.push(tuple(files[:-1]), manager.settings.revision)
    # Push 成功但磁盘不生效。
    candidate = manager.push(files, manager.settings.revision)
    assert manager.get("CANDIDATE", candidate) == files
    assert load_settings(service_path).revision == manager.settings.revision


def test_core_shared_files_are_read_only(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "gw-test-endpoint"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    manager, disk = _manager(service_path)
    core_path = next(
        path for path in disk if path.startswith("templates/business_points_standard.yaml")
    )
    tampered = tuple(
        FileContent(
            path,
            item.data + b"\n# tampered\n" if path == core_path else item.data,
        )
        for path, item in disk.items()
    )
    with pytest.raises(GeneratorError, match="read-only"):
        manager.push(tampered, manager.settings.revision)


def test_invalid_candidate_scenario_rejected(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    manager, disk = _manager(service_path)
    service_rel = manager.settings.service_file.relative_to(manager.settings.root).as_posix()
    document = yaml.safe_load(disk[service_rel].data)
    # 新 Schema 下网关路由按设备展开且 gateway_id 唯一：重复 ID 在配置期必须拒绝
    # （旧 feedback_mapping 双源点映射已无表达路径，OutputTarget 唯一性由加载器兜底）。
    document["scenarios"][0]["gateways"].append(
        {
            "gateway_id": document["scenarios"][0]["gateways"][0]["gateway_id"],
            "endpoint": "127.0.0.1:59998",
            "device_id": "GW01",
        }
    )
    files = tuple(
        FileContent(path, yaml.safe_dump(document).encode() if path == service_rel else item.data)
        for path, item in disk.items()
    )
    with pytest.raises(GeneratorError, match="duplicate Gateway ID"):
        manager.push(files, manager.settings.revision)


def test_apply_scenario_change_is_hot_scenario_file_unchanged_revision(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            interval=1.0,
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    manager, disk = _manager(service_path)
    old_revision = manager.settings.revision
    service_rel = manager.settings.service_file.relative_to(manager.settings.root).as_posix()
    document = yaml.safe_load(disk[service_rel].data)
    document["scenarios"][0]["version"] = "1.0.1"
    document["scenarios"][0]["source"]["interval_seconds"] = 0.25
    files = tuple(
        FileContent(path, yaml.safe_dump(document).encode() if path == service_rel else item.data)
        for path, item in disk.items()
    )
    candidate = manager.push(files, manager.settings.revision)
    new_settings, restart_required = manager.apply(candidate, manager.settings.revision)
    assert restart_required is False
    assert new_settings.scenarios["demo-random"].source["interval_seconds"] == 0.25
    assert new_settings.revision != old_revision
    assert load_settings(service_path).scenarios["demo-random"].source["interval_seconds"] == 0.25


def test_service_file_change_requires_restart(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    manager, disk = _manager(service_path)
    service_rel = manager.settings.service_file.relative_to(manager.settings.root).as_posix()
    document = yaml.safe_load(disk[service_rel].data)
    document["output"]["max_inflight"] = 4
    files = tuple(
        FileContent(path, yaml.safe_dump(document).encode() if path == service_rel else item.data)
        for path, item in disk.items()
    )
    candidate = manager.push(files, manager.settings.revision)
    _, restart_required = manager.apply(candidate, manager.settings.revision)
    assert restart_required is True


def test_immutable_scenario_version_body_change_rejected(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            interval=1.0,
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    manager, disk = _manager(service_path)
    service_rel = manager.settings.service_file.relative_to(manager.settings.root).as_posix()
    document = yaml.safe_load(disk[service_rel].data)
    document["scenarios"][0]["description"] = "changed without version bump"
    files = tuple(
        FileContent(path, yaml.safe_dump(document).encode() if path == service_rel else item.data)
        for path, item in disk.items()
    )
    with pytest.raises(GeneratorError, match="immutable scenario version"):
        manager.push(files, manager.settings.revision)


def test_recovery_rolls_back_active_transaction(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    manager, disk = _manager(service_path)
    root = manager.settings.root
    rel = manager.settings.service_file.relative_to(root).as_posix()
    original = disk[rel].data
    journal = root / ".dg_transaction"
    backup = journal / "backup" / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(original)
    new = journal / "new" / rel
    new.parent.mkdir(parents=True, exist_ok=True)
    new.write_bytes(yaml.safe_dump(copy.deepcopy(yaml.safe_load(original))).encode())
    (journal / "active.json").write_text(
        json.dumps({"existing": [rel], "changed": [rel]}), encoding="utf-8"
    )
    manager.settings.service_file.write_bytes(b"# interrupted partial write\n")
    recover_transaction(root)
    assert not journal.exists()
    assert manager.settings.service_file.read_bytes() == original


def test_committed_marker_wins_on_restart(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    manager, _ = _manager(service_path)
    root = manager.settings.root
    journal = root / ".dg_transaction"
    journal.mkdir()
    (journal / "committed.json").write_text(
        json.dumps({"revision": manager.settings.revision}), encoding="utf-8"
    )
    (journal / "active.json").write_text(
        json.dumps({"existing": [], "changed": []}), encoding="utf-8"
    )
    recover_transaction(root)
    assert not journal.exists()
