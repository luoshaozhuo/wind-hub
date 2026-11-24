"""Durable, version-bound Collector Task intent persistence tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from collector.infrastructure.config.task_intents import SqliteTaskIntentStore

pytestmark = pytest.mark.unit


def test_intent_upsert_and_version_scoped_read(tmp_path: Path) -> None:
    store = SqliteTaskIntentStore(tmp_path / "state" / "task_intents.sqlite3")
    identity_v1 = "collector_tasks_sim_v1.0.0.yaml:1.0.0"
    identity_v2 = "collector_tasks_sim_v1.1.0.yaml:1.1.0"

    store.write_intent("task-1", identity_v1, "START")
    # Re-writing the same key updates rather than duplicating the intent.
    store.write_intent("d1", identity_v1, "STOP")
    store.write_intent("task-2", identity_v1, "START")

    assert store.read_intents(identity_v1) == {"task-1": "STOP", "task-2": "START"}
    # A new immutable Task version must not inherit the previous version's intents.
    assert store.read_intents(identity_v2) == {}


def test_intent_validation_rejects_illegal_input(tmp_path: Path) -> None:
    store = SqliteTaskIntentStore(tmp_path / "intents.sqlite3")
    identity = "collector_tasks_sim_v1.0.0.yaml:1.0.0"
    for task_id, version, state in (
        ("", identity, "START"),
        ("d1", "", "START"),
        ("d1", identity, "PAUSE"),
    ):
        with pytest.raises(ValueError):
            store.write_intent(task_id, version, state)


def test_intent_store_rejects_symlink_path(tmp_path: Path) -> None:
    target = tmp_path / "real.sqlite3"
    target.write_text("", encoding="utf-8")
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        SqliteTaskIntentStore(link)


def test_reader_and_writer_share_identical_instance_key(tmp_path: Path) -> None:
    """The control writer and runtime reader must key on the same Task identity."""
    store = SqliteTaskIntentStore(tmp_path / "intents.sqlite3")
    identity = "collector_tasks_sim_v1.0.0.yaml:1.0.0"
    store.write_intent("source-task", identity, "START")
    assert store.read_intents(identity) == {"source-task": "START"}
    # The same Task under a mismatched identity string stays invisible.
    assert store.read_intents("collector_tasks_sim_v1.0.0.yaml:9.9.9") == {}


def test_legacy_device_scoped_intents_are_not_applied_to_task_groups(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE task_intents (device_id TEXT NOT NULL, "
            "task_version TEXT NOT NULL, desired_state TEXT NOT NULL, "
            "PRIMARY KEY(device_id, task_version))"
        )
        connection.execute(
            "INSERT INTO task_intents VALUES (?, ?, ?)",
            ("device-a", "collector_tasks_old.yaml:1.0.0", "START"),
        )
    store = SqliteTaskIntentStore(database)
    # Device-scoped START cannot be promoted into a group-scoped Task START.
    assert store.read_intents("collector_tasks_old.yaml:1.0.0") == {}
    store.write_intent("new-task", "collector_tasks_new.yaml:1.0.0", "STOP")
    assert store.read_intents("collector_tasks_new.yaml:1.0.0") == {"new-task": "STOP"}
