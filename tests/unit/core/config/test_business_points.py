"""业务点标准目录及显式引用的单元测试。"""

import pytest

from core.application.errors import ConfigError
from core.domain import BusinessPointId, DataType
from core.infrastructure.config.business_points import parse_business_points


def test_parse_shared_business_point() -> None:
    catalog = parse_business_points({
        "business_points": {
            "active_power": {
                "data_type": "float32",
                "unit": "kilowatt",
                "description": "有功功率",
            }
        }
    })
    point = catalog[BusinessPointId("active_power")]
    assert point.data_type is DataType.FLOAT32
    assert point.standard_unit.code.value == "kilowatt"


@pytest.mark.parametrize(
    "definition",
    [
        {"data_type": "float32", "unit": "not-a-unit"},
        {"data_type": "unknown", "unit": "kilowatt"},
        {"data_type": "float32"},
        {"data_type": "float32", "unit": "kilowatt", "extra": 1},
    ],
)
def test_reject_invalid_business_point(definition: dict[str, object]) -> None:
    with pytest.raises(ConfigError):
        parse_business_points({"business_points": {"power": definition}})
