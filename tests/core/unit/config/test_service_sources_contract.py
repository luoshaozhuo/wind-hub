"""出厂服务配置对共享实例与模板的显式引用契约。"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.infrastructure.config import read_yaml

pytestmark = [pytest.mark.unit, pytest.mark.safety]

_ROOT = Path(__file__).resolve().parents[4]
_CONFIGS = _ROOT / "configs"


@pytest.mark.parametrize(
    "service_name",
    ("collector.yaml", "commander.yaml", "industrial_gateway.yaml", "data-generator-service.yaml"),
)
def test_shared_source_paths_are_explicit_and_resolvable(service_name: str) -> None:
    service = read_yaml(_CONFIGS / "services" / service_name)
    assert service["schema_version"] == 1
    assert "configuration" not in service
    sources = service["config_sources"]
    assert set(sources) == {"instances", "templates"}
    assert isinstance(sources["instances"], dict) and sources["instances"]
    assert set(sources["templates"]) == {"root"}

    refs = [*sources["instances"].values(), sources["templates"]["root"]]
    for value in refs:
        assert isinstance(value, str)
        candidate = Path(value)
        assert not candidate.is_absolute()
        assert candidate.parts[0] == "configs"
        assert ".." not in candidate.parts
        assert (_ROOT / candidate).exists(), f"{service_name}: missing {value}"


def test_all_shared_service_templates_are_directories() -> None:
    for service_name in (
        "collector.yaml",
        "commander.yaml",
        "industrial_gateway.yaml",
        "data-generator-service.yaml",
    ):
        service = read_yaml(_CONFIGS / "services" / service_name)
        root = service["config_sources"]["templates"]["root"]
        assert (_ROOT / root).is_dir()
