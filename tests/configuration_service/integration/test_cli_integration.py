"""Every Configuration CLI command against the real repository process."""

from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import pytest
import yaml

from tests.configuration_service.integration.test_repository_rpc import document
from tests.support.commander_mtls import write_commander_service
from tests.support.configuration_live import LiveConfiguration, reserve_port

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


def site_file(live: LiveConfiguration, name: str, version: str) -> Path:
    path = live.root / f"{name}-{version}.yaml"
    path.write_bytes(document(name, version).yaml_bytes)
    return path


def test_configuration_cli_success_matrix(live: LiveConfiguration) -> None:
    source = site_file(live, "cli-site", "1.0.0")
    candidate = live.cli(
        "configuration",
        "candidate",
        "site",
        "cli-site",
        str(source),
        "0",
        "--request-id",
        "cli-create-v1",
    )
    candidate_id = candidate["data"]["candidate_id"]
    assert candidate["data"]["valid"] is True
    replay = live.cli(
        "configuration",
        "candidate",
        "site",
        "cli-site",
        str(source),
        "0",
        "--request-id",
        "cli-create-v1",
    )
    assert replay["data"]["candidate_id"] == candidate_id
    published = live.cli(
        "configuration", "publish", candidate_id, "0", "--request-id", "cli-publish-v1"
    )
    assert published["data"]["revision_id"] == "1"
    version1 = published["data"]["published"][0]["version_id"]
    listed = live.cli("configuration", "list", "--config-type", "site")
    assert listed["data"]["items"][0]["recommended_version_id"] == version1
    versions = live.cli("configuration", "versions", "site", "cli-site")
    assert versions["data"]["versions"][0]["version_id"] == version1
    output = live.root / "downloaded.yaml"
    downloaded = live.cli(
        "configuration",
        "get",
        "site",
        "cli-site",
        str(output),
        "--version-id",
        version1,
    )
    assert downloaded["data"]["version_id"] == version1
    assert output.stat().st_mode & 0o777 == 0o600
    assert yaml.safe_load(output.read_bytes())["site_id"] == "cli-site"
    bundle = live.root / "bundle"
    bundled = live.cli(
        "configuration",
        "bundle",
        "site",
        "cli-site",
        "1",
        str(bundle),
    )
    assert bundled["data"]["files"] == 1
    assert bundle.stat().st_mode & 0o777 == 0o700
    assert yaml.safe_load(next(bundle.glob("*.yaml")).read_bytes())["site_id"] == "cli-site"
    references = live.cli("configuration", "references", version1)
    assert references["data"] == {}
    source2 = site_file(live, "cli-site", "2.0.0")
    candidate2 = live.cli(
        "configuration",
        "candidate",
        "site",
        "cli-site",
        str(source2),
        "1",
        "--request-id",
        "cli-create-v2",
    )
    published2 = live.cli(
        "configuration",
        "publish",
        candidate2["data"]["candidate_id"],
        "1",
        "--request-id",
        "cli-publish-v2",
    )
    version2 = published2["data"]["published"][0]["version_id"]
    recommended = live.cli(
        "configuration",
        "recommend",
        "site",
        "cli-site",
        version1,
        "2",
        "--request-id",
        "cli-recommend-v1",
    )
    assert recommended["data"]["previous_version_id"] == version2
    live.cli(
        "configuration",
        "recommend",
        "site",
        "cli-site",
        version2,
        "3",
        "--request-id",
        "cli-recommend-v2",
    )
    deprecated = live.cli(
        "configuration",
        "deprecate",
        version1,
        "superseded",
        "4",
        "--request-id",
        "cli-deprecate-v1",
    )
    assert deprecated["data"]["deprecated"] is True
    stats = live.cli("configuration", "stats", "--config-type", "site")
    assert stats["data"]["version_count"] == "2"
    backup = live.cli("configuration", "backup", "--label", "cli", "--request-id", "cli-backup")
    task_id = backup["data"]["task_id"]
    backup_replay = live.cli(
        "configuration", "backup", "--label", "cli", "--request-id", "cli-backup"
    )
    assert backup_replay["data"]["task_id"] == task_id
    for _ in range(100):
        task = live.cli("configuration", "backup-status", task_id)
        if task["data"]["task"]["state"] in {"BACKUP_STATE_SUCCEEDED", "BACKUP_STATE_FAILED"}:
            break
        time.sleep(0.05)
    assert task["data"]["task"]["state"] == "BACKUP_STATE_SUCCEEDED"
    assert task["data"]["task"]["integrity_verified"] is True


def test_cli_refuses_to_overwrite_and_query_identity_cannot_publish(
    live: LiveConfiguration,
) -> None:
    existing = live.root / "existing.yaml"
    existing.write_text("keep", encoding="utf-8")
    result = live.cli(
        "configuration",
        "get",
        "site",
        "missing",
        str(existing),
        "--version-id",
        "missing",
        check=False,
    )
    assert result["exit_code"] != 0
    assert existing.read_text(encoding="utf-8") == "keep"
    query_config = live.root / "cli-query.yaml"
    raw = yaml.safe_load(live.cli_path.read_text(encoding="utf-8"))
    raw["services"]["configuration"]["tls_profile"] = "query"
    query_config.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    source = site_file(live, "query-site", "1.0.0")
    denied = live.cli(
        "configuration",
        "candidate",
        "site",
        "query-site",
        str(source),
        "0",
        config=query_config,
        check=False,
    )
    assert denied["exit_code"] != 0
    assert denied["error_code"] == "PERMISSION_DENIED"


def test_cli_apply_does_not_claim_success_when_target_rejects(
    live: LiveConfiguration,
) -> None:
    source = site_file(live, "apply-site", "1.0.0")
    current = live.cli("configuration", "list")["data"]["snapshot_revision_id"]
    staged = live.cli(
        "configuration",
        "candidate",
        "site",
        "apply-site",
        str(source),
        current,
        "--request-id",
        "apply-create",
    )
    published = live.cli(
        "configuration",
        "publish",
        staged["data"]["candidate_id"],
        current,
        "--request-id",
        "apply-publish",
    )
    before = live.bootstrap_path.read_bytes()
    manifest = live.root / "apply.yaml"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": [
                    {
                        "relative_path": live.bootstrap_path.name,
                        "config_type": "site",
                        "config_id": "apply-site",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    result = live.cli(
        "configuration",
        "apply",
        "configuration",
        str(manifest),
        published["data"]["revision_id"],
        check=False,
    )
    assert result["exit_code"] != 0
    assert result["status"] != "SUCCESS"
    assert live.bootstrap_path.read_bytes() == before


def test_cli_apply_devices_to_real_commander_preserves_unlisted_files(
    live: LiveConfiguration,
) -> None:
    root = live.root
    config_tree = root / "commander/configs"
    shutil.copytree(Path(__file__).resolve().parents[3] / "configs", config_tree)
    commander_bootstrap = write_commander_service(
        root / "commander",
        config_tree,
        live.pki,
        grpc_port=reserve_port(),
    )
    cli_document = yaml.safe_load(live.cli_path.read_text(encoding="utf-8"))
    commander_settings = yaml.safe_load(commander_bootstrap.read_bytes())
    cli_document["services"]["commander"] = {
        "grpc_endpoint": f"127.0.0.1:{commander_settings['grpc']['listen_port']}",
        "tls_profile": "publish",
        "manager": "process",
        "target": "commander",
        "process": {
            "argv": [sys.executable, "-m", "commander", "--config", str(commander_bootstrap)],
            "cwd": str(Path(__file__).resolve().parents[3] / "src"),
            "env": {"PYTHONPATH": str(Path(__file__).resolve().parents[3] / "src")},
            "state_dir": str(root / "process-state"),
            "log_file": str(root / "commander.log"),
        },
    }
    live.cli_path.write_text(yaml.safe_dump(cli_document, sort_keys=False), encoding="utf-8")
    started = live.cli("service", "start", "commander", "--yes", check=False)
    assert started["exit_code"] == 0, started
    try:
        sources = [
            (
                "point_table",
                "ads_wtg_gw2mw",
                "configs/templates/point_tables/point_table_ads_wtg_gw2mw_v1.0.0.yaml",
            ),
            (
                "point_table",
                "modbus_wtg_tz2mw",
                "configs/templates/point_tables/point_table_modbus_wtg_tz2mw_v1.0.0.yaml",
            ),
            (
                "device_model",
                "wtg_gw2mw",
                "configs/templates/device_models/device_model_wtg_gw2mw_v1.2.0.yaml",
            ),
            (
                "device_model",
                "wtg_tz2mw",
                "configs/templates/device_models/device_model_wtg_tz2mw_v1.1.0.yaml",
            ),
            ("device", "devices", "configs/instances/devices.yaml"),
        ]
        for index, (config_type, config_id, relative) in enumerate(sources):
            source = Path(__file__).resolve().parents[3] / relative
            if config_type == "point_table":
                fixture = root / f"apply-{config_id}.yaml"
                payload = yaml.safe_load(source.read_bytes())
                payload["device_type"] = "turbine"
                fixture.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
                source = fixture
            revision = live.cli("configuration", "list")["data"]["snapshot_revision_id"]
            staged = live.cli(
                "configuration",
                "candidate",
                config_type,
                config_id,
                str(source),
                revision,
                "--request-id",
                f"apply-real-create-{index}",
            )
            published = live.cli(
                "configuration",
                "publish",
                staged["data"]["candidate_id"],
                revision,
                "--request-id",
                f"apply-real-publish-{index}",
            )
            assert published["status"] == "SUCCESS"
        target_file = config_tree / "instances/devices.yaml"
        unlisted = config_tree / "instances/site.yaml"
        model = config_tree / "templates/device_models/device_model_wtg_gw2mw_v1.2.0.yaml"
        before_unlisted = unlisted.read_bytes()
        before_model = model.read_bytes()
        before_devices = target_file.read_bytes()
        disk = live.cli("config", "get", "commander", "--scope", "disk")
        target_path = next(
            item["relative_path"]
            for item in disk["data"]["files"]
            if item["relative_path"].endswith("instances/devices.yaml")
        )
        manifest = root / "commander-apply.yaml"
        manifest.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "files": [
                        {
                            "relative_path": target_path,
                            "config_type": "device",
                            "config_id": "devices",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        current_revision = live.cli("configuration", "list")["data"]["snapshot_revision_id"]
        applied = live.cli(
            "configuration", "apply", "commander", str(manifest), current_revision, check=False
        )
        assert applied["exit_code"] == 0, applied.get("message")
        assert applied["data"]["applied"] is True
        assert target_file.read_bytes() != before_devices
        assert unlisted.read_bytes() == before_unlisted
        assert model.read_bytes() == before_model
    finally:
        live.cli("service", "stop", "commander", "--yes", check=False)
