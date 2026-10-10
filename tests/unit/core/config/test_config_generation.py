"""完整版本化配置保存与 Reload 差异。"""

from dataclasses import replace
from pathlib import Path
from shutil import copy2

from core.application.config_use_case import ConfigUseCase
from core.infrastructure.config.adapter import YamlConfigAdapter
from core.infrastructure.config.yaml import active_config_dir

ROOT = Path(__file__).resolve().parents[4]
FILES = (
    "system.yaml", "device_models.yaml", "devices.yaml", "points.yaml",
    "business_points.yaml", "tasks.yaml", "sinks.yaml",
)


def test_atomic_generation_swap_and_reload_diff(tmp_path: Path) -> None:
    source = ROOT / "configs" / "example_modbus"
    for name in FILES:
        copy2(source / name, tmp_path / name)
    originals = {name: (tmp_path / name).read_bytes() for name in FILES}
    adapter = YamlConfigAdapter(tmp_path)
    use_case = ConfigUseCase(adapter)
    before = use_case.load()
    new_name = "Updated wind site"
    new_site = replace(before.site, name=new_name)
    task_id, task = next(iter(before.tasks.items()))
    updated = replace(
        before,
        site=new_site,
        tasks={**before.tasks, task_id: replace(task, interval=3.0)},
    )
    adapter.save(updated)
    current, diff = use_case.reload(before)
    assert current.site.name == new_name
    assert current.tasks[task_id].interval == 3.0
    assert diff.site_changed
    assert diff.sections["tasks"].changed == frozenset({task_id})
    assert not diff.sections["devices"].has_changes
    assert active_config_dir(tmp_path) != tmp_path
    assert (tmp_path / ".active-config").exists()
    assert all((tmp_path / filename).read_bytes() == data for filename, data in originals.items())
    assert before.site.name != current.site.name


def test_multiple_generations_are_independently_readable(tmp_path: Path) -> None:
    source = ROOT / "configs" / "example_modbus"
    for name in FILES:
        copy2(source / name, tmp_path / name)
    adapter = YamlConfigAdapter(tmp_path)
    original = adapter.load()
    adapter.save(original)
    generation_one = active_config_dir(tmp_path)
    changed = replace(original, site=replace(original.site, name="second generation"))
    adapter.save(changed)
    generation_two = active_config_dir(tmp_path)
    assert generation_one != generation_two
    assert YamlConfigAdapter(generation_one).load() == original
    assert YamlConfigAdapter(generation_two).load() == changed
