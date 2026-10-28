"""DefinitionQueryService kind 映射测试。"""

import pytest

from web_server.application.config.definitions import DefinitionQueryService

pytestmark = pytest.mark.unit


def test_definition_locations_are_explicit() -> None:
    assert DefinitionQueryService._location("units") == ("units.yaml", "units")
    assert DefinitionQueryService._location("point-tables") == (
        "points.yaml",
        "point_tables",
    )

    with pytest.raises(KeyError):
        DefinitionQueryService._location("device-groups")
