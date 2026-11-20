"""现行 Configuration 全集 CLI 对真实仓库进程的集成验收。"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]
ROOT = Path(__file__).resolve().parents[3]


def _tree(root: Path) -> Path:
    for group in ("instances", "templates", "services"):
        (root / group).mkdir(parents=True)
    return root


def _site(root: Path, config_id: str, version: str, name: str) -> None:
    (root / "instances" / f"{config_id}_v{version}.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "file_version": version,
                "site_id": config_id,
                "name": name,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _sync(live: LiveConfiguration, root: Path, request_id: str) -> dict[str, object]:
    return live.cli(
        "configuration",
        "sync",
        str(root),
        "--yes",
        "--request-id",
        request_id,
    )


def test_sync_complete_tree_query_recommend_deprecate_backup_and_replay(
    live: LiveConfiguration,
) -> None:
    root = _tree(live.root / "sync-minimal")
    _site(root, "site_test", "1.0.0", "alpha-v1")
    _site(root, "site_beta", "1.0.0", "beta-v1")
    first = _sync(live, root, "sync-v1")
    assert first["data"]["revision_id"] == 1
    assert first["data"]["files"] == 2

    replay = _sync(live, root, "sync-v1")
    assert replay["data"]["revision_id"] == 1
    assert replay["data"]["already_applied"] is True
    unchanged = _sync(live, root, "sync-unchanged")
    assert unchanged["data"]["revision_id"] == 1
    assert unchanged["data"]["already_applied"] is True

    listed = live.cli("configuration", "list")
    assert listed["data"]["snapshot_revision_id"] == "1"
    versions = live.cli("configuration", "versions", "site", "site_test")
    assert len(versions["data"]["versions"]) == 1
    output = live.root / "alpha.yaml"
    live.cli("configuration", "get", "site", "site_test", str(output), "--revision-id", "1")
    assert yaml.safe_load(output.read_text())["name"] == "alpha-v1"
    with pytest.raises(AssertionError):
        live.cli("configuration", "get", "site", "site_test", str(output), "--revision-id", "1")

    _site(root, "site_test", "2.0.0", "alpha-v2")
    second = _sync(live, root, "sync-v2")
    assert second["data"]["revision_id"] == 2
    versions = live.cli("configuration", "versions", "site", "site_test")
    by_version = {item["file_version"]: item["version_id"] for item in versions["data"]["versions"]}
    live.cli("configuration", "recommend", "site", "site_test", by_version["2.0.0"], "2")
    recommended = live.cli(
        "configuration", "recommend", "site", "site_test", by_version["1.0.0"], "3"
    )
    assert recommended["data"]["revision_id"] == "4"
    deprecated = live.cli("configuration", "deprecate", by_version["2.0.0"], "superseded", "4")
    assert deprecated["data"]["deprecated"] is True
    stats = live.cli("configuration", "stats")
    assert stats["data"]["version_count"] == "3"

    backup = live.cli("configuration", "backup", "--request-id", "sync-backup")
    task_id = backup["data"]["task_id"]
    for _ in range(100):
        task = live.cli("configuration", "backup-status", task_id)
        if task["data"]["task"]["state"] in {
            "BACKUP_STATE_SUCCEEDED",
            "BACKUP_STATE_FAILED",
        }:
            break
        time.sleep(0.05)
    assert task["data"]["task"]["state"] == "BACKUP_STATE_SUCCEEDED"
    assert task["data"]["task"]["integrity_verified"] is True

    denied = live.cli(
        "configuration",
        "sync",
        str(root),
        "--yes",
        "--request-id",
        "viewer-sync",
        identity="viewer",
        check=False,
    )
    assert denied["exit_code"] != 0
    assert denied["error_code"] == "PERMISSION_DENIED"


def test_real_shipped_sync_keeps_pinned_kafka_100(live: LiveConfiguration) -> None:
    current = live.cli("configuration", "list")["data"]["snapshot_revision_id"]
    published = _sync(live, ROOT / "configs", "sync-shipped-configs")
    assert published["data"]["files"] == 42
    revision = int(published["data"]["revision_id"])
    assert revision > int(current)
    output = live.root / "kafka-history.yaml"
    live.cli(
        "configuration",
        "get",
        "sink",
        "kafka_history",
        str(output),
        "--revision-id",
        str(revision),
    )
    assert yaml.safe_load(output.read_text())["file_version"] == "1.0.0"

    bundle_dir = live.root / "sink-bundle"
    bundle = live.cli("configuration", "bundle", "sink", "sinks", str(revision), str(bundle_dir))
    assert bundle["data"]["files"] == 4
    assert (bundle_dir / "templates/sinks/sink_kafka_history_v1.0.0.yaml").is_file()
    assert not (bundle_dir / "templates/sinks/sink_kafka_history_v1.1.0.yaml").exists()


def test_retired_candidate_publish_apply_commands_remain_unavailable(
    live: LiveConfiguration,
) -> None:
    for arguments in (
        ("configuration", "candidate"),
        ("configuration", "publish"),
        ("configuration", "apply"),
    ):
        result = live.cli(*arguments, check=False)
        assert result["exit_code"] == 2
