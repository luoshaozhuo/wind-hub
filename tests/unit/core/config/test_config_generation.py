"""直接保存 YAML 配置并比较 reload 差异。"""

from dataclasses import replace
from pathlib import Path
from shutil import copy2

from core.application.config_use_case import ConfigUseCase
from core.infrastructure.config.adapter import YamlConfigAdapter

ROOT = Path(__file__).resolve().parents[4]
FILES = (
    "system.yaml",
    "device_models.yaml",
    "devices.yaml",
    "points.yaml",
    "business_points.yaml",
    "tasks.yaml",
    "sinks.yaml",
)


def test_save_updates_files_and_reload_diff(tmp_path: Path) -> None:
    source = ROOT / "configs" / "example_modbus"
    for name in FILES:
        copy2(source / name, tmp_path / name)
    adapter = YamlConfigAdapter(tmp_path)
    use_case = ConfigUseCase(adapter)
    before = use_case.load()
    task_id, task = next(iter(before.tasks.items()))
    updated = replace(
        before,
        site=replace(before.site, name="Updated wind site"),
        tasks={**before.tasks, task_id: replace(task, interval=3.0)},
    )
    adapter.save(updated)
    current, diff = use_case.reload(before)
    assert current.site.name == "Updated wind site"
    assert current.tasks[task_id].interval == 3.0
    assert diff.site_changed
    assert diff.sections["tasks"].changed == frozenset({task_id})
    assert not diff.sections["devices"].has_changes
    assert not (tmp_path / ".active-config").exists()
    assert not (tmp_path / ".config-versions").exists()
    assert before.site.name != current.site.name


def test_repeated_save_uses_same_directory(tmp_path: Path) -> None:
    source = ROOT / "configs" / "example_modbus"
    for name in FILES:
        copy2(source / name, tmp_path / name)
    adapter = YamlConfigAdapter(tmp_path)
    original = adapter.load()
    adapter.save(original)
    changed = replace(original, site=replace(original.site, name="second save"))
    adapter.save(changed)
    assert adapter.load() == changed
    assert not (tmp_path / ".config-versions").exists()
