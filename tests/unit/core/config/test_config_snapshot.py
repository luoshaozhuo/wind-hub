"""一致性配置快照契约测试。"""

from __future__ import annotations

import pytest

from core.application import ConfigError
from core.application.port import ConfigSnapshot, ConfigTopic
from core.infrastructure.config import YamlConfigAdapter

TOPICS = (ConfigTopic.POINTS, ConfigTopic.UNITS)


@pytest.fixture
def site(tmp_path):
    (tmp_path / "points.yaml").write_text(
        "point_tables:\n"
        "  main:\n"
        "    protocol: modbus\n"
        "    points:\n"
        "      - point_id: power\n"
        "        point_groups: [g]\n"
        "        address: {register_type: holding, address: 0}\n",
        encoding="utf-8",
    )
    (tmp_path / "units.yaml").write_text("units: {}\n", encoding="utf-8")
    (tmp_path / "sinks.yaml").write_text("sinks: []\n", encoding="utf-8")
    return tmp_path


def test_snapshot_implements_port_and_reads_declared_topics(site):
    snapshot = YamlConfigAdapter(site).open_snapshot(TOPICS)
    assert isinstance(snapshot, ConfigSnapshot)
    assert "power" in snapshot.read(ConfigTopic.POINTS).tables["main"].points
    assert snapshot.read(ConfigTopic.UNITS).units == {}
    snapshot.verify_unchanged()


def test_snapshot_reads_are_immune_to_concurrent_modification(site):
    """会话内读取解析自 open 时捕获的内容，不混用不同版本。"""
    snapshot = YamlConfigAdapter(site).open_snapshot(TOPICS)
    (site / "points.yaml").write_text("point_tables: {}\n", encoding="utf-8")
    assert "main" in snapshot.read(ConfigTopic.POINTS).tables


def test_verify_unchanged_detects_concurrent_modification(site):
    snapshot = YamlConfigAdapter(site).open_snapshot(TOPICS)
    (site / "points.yaml").write_text("point_tables: {}\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="changed while reading snapshot"):
        snapshot.verify_unchanged()


def test_undeclared_topic_read_is_rejected(site):
    """未声明主题不会隐式读盘；依赖范围显式。"""
    snapshot = YamlConfigAdapter(site).open_snapshot(TOPICS)
    with pytest.raises(ConfigError, match="does not include"):
        snapshot.read(ConfigTopic.SINKS)


def test_empty_topics_are_rejected(site):
    with pytest.raises(ConfigError, match="at least one topic"):
        YamlConfigAdapter(site).open_snapshot(())


def test_snapshot_rejected_when_files_change_during_capture(site, monkeypatch):
    """捕获过程中已读文件被外部修改：双指纹不一致，拒绝创建快照。

    不用 sleep 制造竞态——在第一个主题读取完成后同步改写其文件。
    """
    from core.infrastructure.config import adapter as adapter_module

    original_read = adapter_module.read_yaml_mapping
    poisoned = {"done": False}

    def read_then_modify(path):
        content = original_read(path)
        if not poisoned["done"]:
            poisoned["done"] = True
            # 模拟外部写入：第一个主题已捕获后，其文件被修改。
            (site / "points.yaml").write_text("point_tables: {}\n", encoding="utf-8")
        return content

    monkeypatch.setattr(adapter_module, "read_yaml_mapping", read_then_modify)
    with pytest.raises(ConfigError, match="changed while capturing snapshot"):
        YamlConfigAdapter(site).open_snapshot(TOPICS)


def test_snapshot_capture_consistent_when_unchanged(site):
    """正常捕获：创建成功，重复读取语义一致，verify_unchanged 通过。"""
    snapshot = YamlConfigAdapter(site).open_snapshot(TOPICS)
    first = snapshot.read(ConfigTopic.POINTS)
    second = snapshot.read(ConfigTopic.POINTS)
    assert first == second
    snapshot.verify_unchanged()


def test_unrelated_topic_change_does_not_affect_snapshot(site):
    """只声明 POINTS/UNITS 的快照：SINKS 变化不影响一致性，也不加载 SINKS。"""
    snapshot = YamlConfigAdapter(site).open_snapshot(TOPICS)
    (site / "sinks.yaml").write_text("sinks: [{name: x}]\n", encoding="utf-8")
    snapshot.verify_unchanged()
    with pytest.raises(ConfigError, match="does not include"):
        snapshot.read(ConfigTopic.SINKS)
