"""Shipped Service → Instance → Template selection contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.configuration.local_selection import select_local_service
from core.infrastructure.config import read_yaml

pytestmark = [pytest.mark.unit, pytest.mark.safety]

_CONFIGS = Path(__file__).resolve().parents[4] / "configs"

_SERVICES = (
    "service_collector-realtime_v1.1.0.yaml",
    "service_collector-reliable_v1.0.0.yaml",
    "service_commander_v1.4.0.yaml",
    "service_gateway_v1.2.0.yaml",
    "service_data-generator_v1.1.0.yaml",
    "service_sink-west_v1.2.0.yaml",
)


@pytest.mark.parametrize("service_name", _SERVICES)
def test_shared_source_selection_is_exact_and_versioned(service_name: str) -> None:
    path = _CONFIGS / "services" / service_name
    service = read_yaml(path)
    assert service["schema_version"] in {1, 2}
    assert service["service_id"]
    sources = service["config_sources"]
    assert set(sources) == {"instances"}
    assert isinstance(sources["instances"], dict) and sources["instances"]
    selection = select_local_service(path)
    assert set(selection.instances) == set(sources["instances"])
    for role, selected in selection.instances.items():
        assert selected.is_file()
        assert selected.suffix == ".yaml"
        assert "_v" in selected.stem
        assert selected.parent == _CONFIGS / "instances", role
    assert selection.templates == _CONFIGS / "templates"


@pytest.mark.parametrize("service_name", _SERVICES)
def test_service_does_not_reference_template_paths(service_name: str) -> None:
    service = read_yaml(_CONFIGS / "services" / service_name)
    for identity in service["config_sources"]["instances"].values():
        assert isinstance(identity, str)
        assert identity
        assert "/" not in identity
        assert "\\" not in identity
        assert identity not in {".", "..", "latest"}
