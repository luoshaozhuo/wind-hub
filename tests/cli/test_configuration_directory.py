"""Configuration 完整目录的固定版本选择与 fail-closed 回归。"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from cli.configuration_directory import _explicit_references, read_complete_directory

ROOT = Path(__file__).resolve().parents[2]


def _write(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _sink_instance(version: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "file_version": "1.0.0",
        "sinks": {"history": {"template_id": "kafka_history", "template_version": version}},
    }


def _sink_template(version: str, *, name: str | None = None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "file_version": version,
        "template_id": "kafka_history",
        "sink_type": "kafka",
        "config": {"marker": name or version},
    }


def test_shipped_directory_pins_kafka_100_and_builds_template_references() -> None:
    documents = read_complete_directory(ROOT / "configs")
    assert len(documents) == 42
    kafka = [item for item in documents if item.config_id == "kafka_history"]
    assert [(item.config_type, item.file_version) for item in kafka] == [
        ("sink", (1, 0, 0)),
        ("sink", (1, 1, 0)),
    ]
    for config_type, config_id, expected in (
        ("sink", "sinks", 3),
        ("delivery", "deliveries", 2),
    ):
        item = next(
            document
            for document in documents
            if (document.config_type, document.config_id) == (config_type, config_id)
        )
        assert len(_explicit_references(item)) == expected


def test_missing_explicitly_pinned_template_is_rejected(tmp_path: Path) -> None:
    for group in ("instances", "templates", "services"):
        (tmp_path / group).mkdir()
    _write(tmp_path / "instances" / "sinks.yaml", _sink_instance("1.0.0"))
    with pytest.raises(ValueError, match="missing pinned Configuration"):
        read_complete_directory(tmp_path)


def test_different_current_references_can_use_distinct_versions(tmp_path: Path) -> None:
    for group in ("instances", "templates", "services"):
        (tmp_path / group).mkdir()
    _write(tmp_path / "instances" / "sinks.yaml", _sink_instance("1.0.0"))
    _write(tmp_path / "instances" / "sink_other.yaml", _sink_instance("1.1.0"))
    for version in ("1.0.0", "1.1.0"):
        _write(
            tmp_path / "templates" / "sinks" / f"sink_kafka_history_v{version}.yaml",
            _sink_template(version),
        )
    documents = read_complete_directory(tmp_path)
    assert len(documents) == 4
    assert {item.file_version for item in documents if item.config_id == "kafka_history"} == {
        (1, 0, 0),
        (1, 1, 0),
    }


def test_same_identity_version_with_different_content_is_rejected(tmp_path: Path) -> None:
    for group in ("instances", "templates", "services"):
        (tmp_path / group).mkdir()
    _write(
        tmp_path / "templates" / "sinks" / "sink_kafka_history_v1.0.0.yaml",
        _sink_template("1.0.0", name="first"),
    )
    _write(
        tmp_path / "templates" / "sinks" / "sink_other_v1.0.0.yaml",
        _sink_template("1.0.0", name="different"),
    )
    with pytest.raises(ValueError, match="duplicate immutable Configuration version"):
        read_complete_directory(tmp_path)


def test_same_identity_version_duplicate_paths_are_rejected(tmp_path: Path) -> None:
    for group in ("instances", "templates", "services"):
        (tmp_path / group).mkdir()
    document = _sink_template("1.0.0")
    _write(tmp_path / "templates/sinks/sink_kafka_history_v1.0.0.yaml", document)
    _write(tmp_path / "templates/sinks/sink_other_v1.0.0.yaml", document)
    with pytest.raises(ValueError, match="duplicate immutable Configuration version"):
        read_complete_directory(tmp_path)
