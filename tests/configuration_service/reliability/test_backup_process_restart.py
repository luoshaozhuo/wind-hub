"""Crash recovery of a CLI-managed real service and durable backup task."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import time
import uuid

import pytest

from configuration_service.infrastructure.adapters.outbound.sqlite.repository import (
    SQLiteRepository,
)
from tests.support.configuration_live import reserve_port, write_live_configuration

pytestmark = [pytest.mark.reliability, pytest.mark.real_service]


def test_killed_process_marks_unfinished_backup_interrupted_and_releases_lock(
    tmp_path,
) -> None:
    live = write_live_configuration(tmp_path, reserve_port())
    live.cli("service", "start", "configuration", "--yes")
    task_id = uuid.uuid4().hex
    with sqlite3.connect(live.database_path) as connection:
        connection.execute(
            "INSERT INTO backup_tasks "
            "(task_id, actor_id, request_id, state, created_at) "
            "VALUES (?, 'admin', ?, 'RUNNING', '2026-01-01T00:00:00+00:00')",
            (task_id, task_id),
        )
    state_file = live.root / "process-state/configuration.json"
    pid = json.loads(state_file.read_text(encoding="utf-8"))["pid"]
    os.kill(pid, signal.SIGKILL)
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    live.cli("service", "restart", "configuration", "--yes")
    try:
        task = live.cli("config", "backup-status", task_id)
        assert task["data"]["task"]["state"] == "BACKUP_STATE_INTERRUPTED"
        assert task["data"]["task"]["finished_at"]
    finally:
        live.cli("service", "stop", "configuration", "--yes")
    repository = SQLiteRepository(live.database_path, application_version="test")
    repository.initialize()
    repository.close()


def test_full_root_publish_survives_real_process_restarts(tmp_path) -> None:
    import yaml

    live = write_live_configuration(tmp_path, reserve_port())
    live.cli("service", "start", "configuration", "--yes")
    try:
        root = tmp_path / "restart-root"
        # Task Templates were retired; publish a versioned collector_tasks Instance.
        instances = root / "instances"
        instances.mkdir(parents=True)
        (instances / "collector_tasks_restart_v1.0.0.yaml").write_text(
            yaml.safe_dump(
                {
                    "schema_version": 1,
                    "file_version": "1.0.0",
                    "tasks": {},
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        published = live.cli(
            "config",
            "publish",
            "--root",
            str(root),
            "--request-id",
            "restart-publish",
        )
        assert int(published["data"]["revision_id"]) == 1
        live.cli("service", "restart", "configuration", "--yes")
        # The durable revision and its manifest survive the process restart.
        revision = live.cli("config", "revision")
        assert int(revision["data"]["revision_id"]) == 1
        live.cli("service", "restart", "configuration", "--yes")
        # Replaying the identical manifest after restart is idempotent even
        # without reusing the original request id (the CLI always addresses the
        # current revision): no new revision is created.
        replay = live.cli("config", "publish", "--root", str(root))
        assert replay["data"]["already_applied"] is True
        assert int(replay["data"]["revision_id"]) == 1
    finally:
        live.cli("service", "stop", "configuration", "--yes")


def test_corrupt_database_cli_start_fails_closed(tmp_path) -> None:
    live = write_live_configuration(tmp_path, reserve_port())
    live.database_path.parent.mkdir(parents=True)
    live.database_path.write_bytes(b"not-a-sqlite-database")
    started = live.cli("service", "start", "configuration", "--yes", check=False)
    try:
        assert started["exit_code"] != 0
        assert started["status"] != "SUCCESS"
    finally:
        live.cli("service", "stop", "configuration", "--yes", check=False)
