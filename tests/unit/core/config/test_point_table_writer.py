"""领域点表展开与继承式 YAML 输出测试。"""

from core.domain import (
    BusinessPointId,
    Point,
    PointAccess,
    PointTable,
    PointTableId,
    Protocol,
)
from core.infrastructure.config.point_table_writer import dump_point_tables
from tests.support.units import KILOWATT_UNIT


def test_expanded_child_serializes_as_parent_difference() -> None:
    base_point = Point(
        point_id="power",
        business_point_id=BusinessPointId("power"),
        source_unit=KILOWATT_UNIT,
        access=PointAccess.READ,
        ext={"data_type": "int32", "type": "input", "address": 10},
        point_groups=("all",),
    )
    changed_point = Point(
        point_id="power",
        business_point_id=BusinessPointId("power"),
        source_unit=KILOWATT_UNIT,
        access=PointAccess.READ,
        ext={"data_type": "int32", "type": "input", "address": 20},
        point_groups=("all",),
    )
    parent = PointTable(PointTableId("base"), Protocol("modbus"), {"power": base_point})
    child = PointTable(
        PointTableId("child"),
        Protocol("modbus"),
        {"power": changed_point},
        parent_id=PointTableId("base"),
    )
    yaml = dump_point_tables(
        {parent.point_table_id: parent, child.point_table_id: child},
    )
    saved = yaml["point_tables"]["child"]
    assert saved["parent_id"] == "base"
    assert saved["points"] == {"power": {"address": {"type": "input", "address": 20}}}
    assert "protocol" not in saved


def test_parent_id_does_not_expand_or_modify_child() -> None:
    parent = PointTable(PointTableId("base"), Protocol("modbus"), {})
    child = PointTable(
        PointTableId("child"), Protocol("modbus"), {}, parent_id=parent.point_table_id
    )
    assert child.points == {}
    assert child.parent_id == parent.point_table_id
