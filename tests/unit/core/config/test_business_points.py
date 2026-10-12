"""业务点标准目录及显式引用的单元测试。"""

import pytest

from core.application.errors import ConfigError
from core.domain import BusinessPointId, DataType
from core.infrastructure.config.business_points import parse_business_points
from tests.support.units import minimal_unit_catalog


def test_parse_shared_business_point() -> None:
    catalog = parse_business_points(
        {
            "business_points": {
                "active_power": {
                    "data_type": "float32",
                    "unit_id": "kilowatt",
                    "description": "有功功率",
                }
            }
        },
        minimal_unit_catalog(),
    )
    point = catalog[BusinessPointId("active_power")]
    assert point.data_type is DataType.FLOAT32
    assert point.standard_unit.unit_id == "kilowatt"


@pytest.mark.parametrize(
    "definition",
    [
        {"data_type": "float32", "unit_id": "not-a-unit"},
        {"data_type": "unknown", "unit_id": "kilowatt"},
        {"data_type": "float32"},
        {"data_type": "float32", "unit_id": "kilowatt", "extra": 1},
    ],
)
def test_reject_invalid_business_point(definition: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse_business_points({"business_points": {"power": definition}}, minimal_unit_catalog())
