"""Every Configuration CLI command against the real tokenized repository process.

The legacy single-file ``configuration candidate/apply`` workflow was retired in
v1_16 together with internal mTLS; these cases drive the replacement full-root
``config`` command surface through independent CLI subprocesses.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
import yaml

from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _task_root(live: LiveConfiguration, versions: tuple[str, ...]) -> Path:
    directory = live.root / f"root-{'-'.join(versions)}"
    tasks = directory / "instances"
    tasks.mkdir(parents=True, exist_ok=True)
    for version in versions:
        (tasks / f"collector_tasks_cli_v{version}.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "file_version": version,
                    "tasks": {},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    return directory


def _point_table_root(live: LiveConfiguration) -> Path:
    directory = live.root / "point-table-root"
    (directory / "templates/point_tables").mkdir(parents=True)
    for filename in ("units_standard_v1.2.0.yaml", "business_points_standard_v1.4.0.yaml"):
        shutil.copyfile(
            PROJECT_ROOT / "configs/templates" / filename,
            directory / "templates" / filename,
        )
    (directory / "templates/point_tables/point_table_wtg_v1.0.0.yaml").write_text(
        "schema_version: 1\nfile_version: 1.0.0\ntemplate_id: wtg\n"
        "device_type: turbine\nprotocol: modbus\npoints: {}\n",
        encoding="utf-8",
    )
    return directory


def test_configuration_cli_success_matrix(live: LiveConfiguration) -> None:
    root_v1 = _task_root(live, ("1.0.0",))
    validated = live.cli("config", "publish", "--root", str(root_v1), "--validate-only")
    assert validated["data"]["valid"] is True
    assert validated["data"]["published"] is False

    published = live.cli("config", "publish", "--root", str(root_v1))
    assert int(published["data"]["revision_id"]) == 1
    assert published["data"]["file_count"] == 1
    replay = live.cli("config", "publish", "--root", str(root_v1))
    assert replay["data"]["already_applied"] is True
    assert replay["data"]["revision_id"] == published["data"]["revision_id"]

    revision = live.cli("config", "revision")
    assert int(revision["data"]["revision_id"]) == 1
    assert len(revision["data"]["manifest_sha256"]) == 64

    listed = live.cli("config", "list", "--config-type", "collector_tasks")
    version1 = listed["data"]["items"][0]["recommended_version_id"]
    versions = live.cli("config", "versions", "collector_tasks", "cli")
    assert versions["data"]["versions"][0]["version_id"] == version1

    output = live.root / "downloaded.yaml"
    downloaded = live.cli(
        "config",
        "get",
        "collector_tasks",
        "--config-id",
        "cli",
        "--output",
        str(output),
        "--version-id",
        version1,
    )
    assert downloaded["data"]["version_id"] == version1
    assert output.stat().st_mode & 0o777 == 0o600
    assert yaml.safe_load(output.read_bytes())["file_version"] == "1.0.0"

    bundle = live.root / "bundle"
    bundled = live.cli("config", "bundle", "collector_tasks", "cli", "1", str(bundle))
    assert bundled["data"]["files"] == 1
    assert bundle.stat().st_mode & 0o777 == 0o700
    assert yaml.safe_load(next(bundle.rglob("*.yaml")).read_bytes())["file_version"] == "1.0.0"

    references = live.cli("config", "references", version1)
    assert references["data"].get("edges", []) == []

    # A second revision keeps the immutable v1 next to the new v2 in the root.
    root_v2 = _task_root(live, ("1.0.0", "2.0.0"))
    live.cli("config", "publish", "--root", str(root_v2))
    versions_after = live.cli(
        "config", "versions", "collector_tasks", "cli", "--include-deprecated"
    )
    version_ids = [item["version_id"] for item in versions_after["data"]["versions"]]
    assert len(version_ids) == 2
    version2 = next(
        item["version_id"]
        for item in versions_after["data"]["versions"]
        if item["version_id"] != version1
    )
    recommended = live.cli(
        "config",
        "recommend",
        "collector_tasks",
        "cli",
        version1,
        "2",
        "--request-id",
        "cli-recommend-v1",
    )
    assert recommended["data"]["previous_version_id"] == version2
    live.cli(
        "config",
        "recommend",
        "collector_tasks",
        "cli",
        version2,
        "3",
        "--request-id",
        "cli-recommend-v2",
    )

    # v1 can only be deprecated once it leaves the active directory.
    root_v2_only = _task_root(live, ("2.0.0",))
    live.cli("config", "publish", "--root", str(root_v2_only))
    current = int(live.cli("config", "revision")["data"]["revision_id"])
    deprecated = live.cli(
        "config",
        "deprecate",
        version1,
        "superseded",
        str(current),
        "--request-id",
        "cli-deprecate-v1",
    )
    assert deprecated["data"]["deprecated"] is True

    stats = live.cli("config", "stats")
    assert int(stats["data"]["version_count"]) == 2
    assert int(stats["data"]["db_schema_version"]) == 2

    backup = live.cli("config", "backup", "--label", "cli", "--request-id", "cli-backup")
    task_id = backup["data"]["task_id"]
    backup_replay = live.cli("config", "backup", "--label", "cli", "--request-id", "cli-backup")
    assert backup_replay["data"]["task_id"] == task_id
    for _ in range(100):
        task = live.cli("config", "backup-status", task_id)
        if task["data"]["task"]["state"] in {
            "BACKUP_STATE_SUCCEEDED",
            "BACKUP_STATE_FAILED",
        }:
            break
        time.sleep(0.05)
    assert task["data"]["task"]["state"] == "BACKUP_STATE_SUCCEEDED"
    assert task["data"]["task"]["integrity_verified"] is True

    point_root = _point_table_root(live)
    live.cli("config", "publish", "--root", str(point_root))
    point_bundle = live.cli("config", "point-table-bundle", "point_table_wtg_v1.0.0")
    assert point_bundle["data"]["point_table_id"] == "point_table_wtg_v1.0.0"
    assert point_bundle["data"]["documents"] == 3
    assert len(point_bundle["data"]["manifest_sha256"]) == 64


def test_cli_refuses_to_overwrite_and_query_identity_cannot_publish(
    live: LiveConfiguration, monkeypatch: pytest.MonkeyPatch
) -> None:
    existing = live.root / "existing.yaml"
    existing.write_text("keep", encoding="utf-8")
    result = live.cli(
        "config",
        "get",
        "collector_tasks",
        "--config-id",
        "missing",
        "--output",
        str(existing),
        "--version-id",
        "missing",
        check=False,
    )
    assert result["exit_code"] != 0
    assert existing.read_text(encoding="utf-8") == "keep"

    root = _task_root(live, ("1.0.0",))
    monkeypatch.setenv("WIND_HUB_CONFIG_CLI_ADMIN_TOKEN", live.tokens["viewer"])
    denied = live.cli("config", "publish", "--root", str(root), check=False)
    assert denied["exit_code"] != 0
    assert denied["error_code"] == "PERMISSION_DENIED"


def test_publish_rejects_unsafe_trees_without_side_effects(
    live: LiveConfiguration,
) -> None:
    revision_before = int(live.cli("config", "revision")["data"]["revision_id"])
    root = _task_root(live, ("9.9.9",))
    linked = root / "instances/linked.yaml"
    linked.symlink_to(next((root / "instances").glob("*.yaml")))
    rejected_link = live.cli("config", "publish", "--root", str(root), check=False)
    assert rejected_link["exit_code"] != 0
    linked.unlink()

    noncanonical = root / "instances/legacy.yml"
    noncanonical.write_text("schema_version: 1\n", encoding="utf-8")
    rejected_suffix = live.cli("config", "publish", "--root", str(root), check=False)
    assert rejected_suffix["exit_code"] != 0

    # Neither rejected admission created a new revision.
    revision_after = int(live.cli("config", "revision")["data"]["revision_id"])
    assert revision_after == revision_before
