"""DefinitionsUseCase kind 映射测试。"""

import pytest

from wind_hub_server.application.usecase.definitions import DefinitionsUseCase


def test_definition_locations_are_explicit() -> None:
    assert DefinitionsUseCase._location("units") == ("units.yaml", "units")
    assert DefinitionsUseCase._location("point-tables") == (
        "points.yaml",
        "point_tables",
    )

    with pytest.raises(KeyError):
        DefinitionsUseCase._location("device-groups")
