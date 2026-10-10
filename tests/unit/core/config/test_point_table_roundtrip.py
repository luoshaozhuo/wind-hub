"""完整领域点表保存后重新加载的往返测试。"""

from pathlib import Path
from shutil import copyfile

from core.infrastructure.config.full_adapter import FullYamlConfigAdapter


ROOT = Path(__file__).resolve().parents[4]


def test_point_tables_yaml_round_trip(tmp_path: Path) -> None:
    source = ROOT / "configs" / "template"
    for filename in (
        "system.yaml", "device_models.yaml", "devices.yaml",
        "points.yaml", "tasks.yaml", "sinks.yaml", "business_points.yaml",
    ):
        copyfile(source / filename, tmp_path / filename)

    adapter = FullYamlConfigAdapter(tmp_path)
    before = adapter.load()
    adapter.save_point_tables(before)
    after = adapter.load()

    assert before.point_tables == after.point_tables
    assert before.point_meta == after.point_meta
    assert all(
        before.point_tables[key].parent_id == after.point_tables[key].parent_id
        for key in before.point_tables
    )

    written = (tmp_path / "points.yaml").read_text(encoding="utf-8")
    assert "extends: beckhoff_base_v1" in written
    assert "business_point_id" in written


def test_point_table_save_reload_keeps_changed_and_removed_points(tmp_path: Path) -> None:
    from dataclasses import replace
    from core.domain import PointTableId

    source = ROOT / "configs" / "template"
    for filename in (
        "system.yaml", "device_models.yaml", "devices.yaml",
        "points.yaml", "tasks.yaml", "sinks.yaml", "business_points.yaml",
    ):
        copyfile(source / filename, tmp_path / filename)
    adapter = FullYamlConfigAdapter(tmp_path)
    original = adapter.load()
    parent = original.point_tables[PointTableId("beckhoff_base_v1")]
    child = original.point_tables[PointTableId("beckhoff_wtg_v1")]
    # 子表删除一个继承点，同时保留地址覆盖和自己新增的点。
    assert "rotor_speed" in parent.points
    changed = replace(child, points={
        key: value for key, value in child.points.items() if key != "rotor_speed"
    })
    new_tables = {**original.point_tables, changed.point_table_id: changed}
    changed_snapshot = replace(original, point_tables=new_tables)
    adapter.save_point_tables(changed_snapshot)
    restored = adapter.load()
    assert "rotor_speed" not in restored.point_tables[child.point_table_id].points
    assert restored.point_tables[child.point_table_id] == changed
    assert restored.point_tables[parent.point_table_id] == parent
    assert "remove_points:" in (tmp_path / "points.yaml").read_text(encoding="utf-8")
