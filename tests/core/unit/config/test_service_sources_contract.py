"""Shipped Service → Instance → Template selection contract tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.configuration.local_selection import select_local_service
from core.infrastructure.config import read_yaml

pytestmark = [pytest.mark.unit, pytest.mark.safety]

# 校验生产激活根：每个调用方按 Deployment 选择唯一 Service/Instance 版本。
# configs/ 保留全部历史 Deployment，同一 Service 版本可能被多个历史 Deployment
# 选中，无法再满足 select_local_service 的"唯一 Deployment"契约，因此不再作为
# 无 deployment 上下文的取样来源。
_CONFIGS = Path(__file__).resolve().parents[4] / "deploy" / "config_roots" / "prod-a"

_SERVICES = tuple(
    sorted(
        path.name
        for path in (_CONFIGS / "services").glob("service_*.yaml")
        if "instances" in (read_yaml(path).get("config_sources") or {})
    )
)


@pytest.mark.parametrize("service_name", _SERVICES)
def test_shared_source_selection_is_exact_and_versioned(service_name: str) -> None:
    path = _CONFIGS / "services" / service_name
    service = read_yaml(path)
    assert service["schema_version"] in {1, 2, 3}
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
