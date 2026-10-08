"""Shared topic-scoped configuration reader contract tests."""

from __future__ import annotations

import hashlib

import pytest

from core.application import ConfigError
from core.application.port.config import (
    CollectorConfigReader,
    CommanderConfigReader,
)
from core.infrastructure.config import YamlConfigReader, fingerprint_config_set


@pytest.fixture
def site(tmp_path):
    for name in (
        "system",
        "device_models",
        "devices",
        "points",
        "units",
        "tasks",
        "sinks",
    ):
        (tmp_path / f"{name}.yaml").write_text(
            f"section: {name}\n",
            encoding="utf-8",
        )
    return tmp_path


def test_topic_reader_implements_composed_ports(site):
    reader = YamlConfigReader(site)
    assert isinstance(reader, CollectorConfigReader)
    assert isinstance(reader, CommanderConfigReader)
    assert reader.read_system() == {"section": "system"}
    assert reader.read_device_models() == {"section": "device_models"}
    assert reader.read_devices() == {"section": "devices"}
    assert reader.read_points() == {"section": "points"}
    assert reader.read_units() == {"section": "units"}
    assert reader.read_tasks() == {"section": "tasks"}
    assert reader.read_sinks() == {"section": "sinks"}


def test_reading_missing_invalid_or_empty_file_is_rejected(tmp_path):
    reader = YamlConfigReader(tmp_path)
    with pytest.raises(ConfigError, match="not found"):
        reader.read_devices()
    path = tmp_path / "devices.yaml"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="root must be a mapping"):
        reader.read_devices()
    path.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="Empty configuration"):
        reader.read_devices()
    path.write_text("devices: [\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="Invalid YAML"):
        reader.read_devices()


def test_fingerprint_preserves_legacy_nul_delimiters(site):
    expected = hashlib.sha256()
    for path in sorted(site.glob("*.yaml")):
        expected.update(path.name.encode("utf-8"))
        expected.update(b"\0")
        expected.update(path.read_bytes())
        expected.update(b"\0")
    assert fingerprint_config_set(site) == expected.hexdigest()
    assert YamlConfigReader(site).fingerprint() == expected.hexdigest()
    history = site / ".history"
    history.mkdir()
    (history / "ignored.yaml").write_text("changed: true", encoding="utf-8")
    assert fingerprint_config_set(site) == expected.hexdigest()
