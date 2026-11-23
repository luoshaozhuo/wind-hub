"""Crash recovery of a CLI-managed real service and durable backup task."""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

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


def test_concurrent_cli_starts_keep_one_owner_and_reject_foreign_configuration(tmp_path) -> None:
    """Independent CLI invocations must agree on the owner of the managed endpoint."""
    port = reserve_port()
    owner = write_live_configuration(tmp_path / "owner", port)
    foreign = write_live_configuration(tmp_path / "foreign", port)
    foreign.database_path.parent.mkdir(parents=True)
    foreign.database_path.write_bytes(b"not-a-sqlite-database")
    state_file = owner.root / "process-state/configuration.json"
    foreign_state = foreign.root / "process-state/configuration.json"

    def start_owner():
        started_at = time.monotonic()
        outcome = owner.cli("service", "start", "configuration", "--yes")
        return outcome, time.monotonic() - started_at

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            starts = [pool.submit(start_owner) for _ in range(3)]
            outcomes, elapsed = zip(*(start.result() for start in starts), strict=True)
        processes = [outcome["data"]["process"] for outcome in outcomes]
        pid = json.loads(state_file.read_text(encoding="utf-8"))["pid"]
        assert {process["pid"] for process in processes} == {pid}
        assert sum(bool(process.get("already_running")) for process in processes) == 2
        assert all(outcome["data"]["business"]["health"] != "UNKNOWN" for outcome in outcomes)
        for _ in range(3):
            rejected = foreign.cli("service", "start", "configuration", "--yes", check=False)
            assert rejected["exit_code"] != 0
            assert "endpoint is already occupied" in json.dumps(rejected)
            assert not foreign_state.exists()
        assert json.loads(state_file.read_text(encoding="utf-8"))["pid"] == pid
        assert not (foreign.root / "configuration.log").exists()
        print(
            f"Configuration port={port} owner_pid={pid} parallel_starts=3 "
            f"foreign_rejections=3 cli_wall_seconds={[round(delay, 3) for delay in elapsed]}"
        )
    finally:
        owner.cli("service", "stop", "configuration", "--yes", check=False)
        foreign.cli("service", "stop", "configuration", "--yes", check=False)
    assert not state_file.exists()
    assert not foreign_state.exists()
