"""完整快照差异测试。"""

from dataclasses import replace
from pathlib import Path

from core.application.config_diff import diff_config_snapshots
from core.domain import Site
from core.infrastructure.config.adapter import YamlConfigAdapter

ROOT = Path(__file__).resolve().parents[4]


def test_snapshot_diff_no_changes() -> None:
    config = YamlConfigAdapter(ROOT / "configs" / "example_modbus").load()
    diff = diff_config_snapshots(config, config)
    assert not diff.has_changes
    assert not any(section.has_changes for section in diff.sections.values())


def test_snapshot_diff_device_and_site() -> None:
    config = YamlConfigAdapter(ROOT / "configs" / "example_modbus").load()
    devices = dict(config.site.devices)
    removed = next(iter(devices))
    del devices[removed]
    updated = replace(config, site=Site(config.site.site_id, "新版风电场", devices))
    diff = diff_config_snapshots(config, updated)
    assert diff.has_changes
    assert diff.site_changed
    assert diff.sections["devices"].removed == frozenset({str(removed)})
    assert not diff.sections["tasks"].has_changes


def test_snapshot_diff_task_change() -> None:
    config = YamlConfigAdapter(ROOT / "configs" / "example_modbus").load()
    task_id, task = next(iter(config.tasks.items()))
    modified = replace(config, tasks={**config.tasks, task_id: replace(task, interval=2.0)})
    diff = diff_config_snapshots(config, modified)
    assert diff.sections["tasks"].changed == frozenset({task_id})
    assert not diff.site_changed
