"""领域点表展开与继承式 YAML 输出测试。"""

from core.domain import (
    BusinessPointId,
    Point,
    PointAccess,
    PointMeta,
    PointTable,
    PointTableId,
    Protocol,
    KILOWATT,
)
from core.infrastructure.config.point_table_writer import dump_point_tables


def test_expanded_child_serializes_as_parent_difference() -> None:
    base_point = Point(
        point_id="power",
        business_point_id=BusinessPointId("power"),
        source_unit=KILOWATT,
        access=PointAccess.READ,
        ext={"data_type": "int32", "type": "input", "address": 10},
    )
    changed_point = Point(
        point_id="power",
        business_point_id=BusinessPointId("power"),
        source_unit=KILOWATT,
        access=PointAccess.READ,
        ext={"data_type": "int32", "type": "input", "address": 20},
    )
    parent = PointTable(PointTableId("base"), Protocol("modbus"), {"power": base_point})
    child = PointTable(
        PointTableId("child"),
        Protocol("modbus"),
        {"power": changed_point},
        parent_id=PointTableId("base"),
    )
    metadata = PointMeta(variable_name="power", point_groups=("all",))
    yaml = dump_point_tables(
        {parent.point_table_id: parent, child.point_table_id: child},
        {parent.point_table_id: {"power": metadata}, child.point_table_id: {"power": metadata}},
    )
    saved = yaml["point_tables"]["child"]
    assert saved["extends"] == "base"
    assert saved["points"] == [{"point_id": "power", "address": {"type": "input", "address": 20}}]
    assert "protocol" not in saved


def test_parent_id_does_not_expand_or_modify_child() -> None:
    parent = PointTable(PointTableId("base"), Protocol("modbus"), {})
    child = PointTable(PointTableId("child"), Protocol("modbus"), {}, parent_id=parent.point_table_id)
    assert child.points == {}
    assert child.parent_id == parent.point_table_id
