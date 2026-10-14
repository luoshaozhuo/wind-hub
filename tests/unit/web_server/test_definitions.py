"""DefinitionQueryService kind 映射测试。"""

import pytest

from wind_hub_server.application.config.definitions import DefinitionQueryService


def test_definition_locations_are_explicit() -> None:
    assert DefinitionQueryService._location("units") == ("units.yaml", "units")
    assert DefinitionQueryService._location("point-tables") == (
        "points.yaml",
        "point_tables",
    )

    with pytest.raises(KeyError):
        DefinitionQueryService._location("device-groups")
