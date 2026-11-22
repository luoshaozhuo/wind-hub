"""Reject unselected members of a cached configuration generation."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.infrastructure.configuration_cache import _verify_cached_files


@pytest.mark.parametrize(
    "extra",
    [
        "configs/templates/unused",
        "unselected.yaml",
        "unselected_dir",
        "configs/unselected_dir",
    ],
)
def test_cached_generation_rejects_unselected_entries(tmp_path: Path, extra: str) -> None:
    generation = tmp_path / "1.0.0" / ("a" * 64)
    configs = generation / "configs"
    service = configs / "services" / "service_commander_v1.0.0.yaml"
    service.parent.mkdir(parents=True)
    service.write_bytes(b"service")
    (configs / "deployments").mkdir()

    _verify_cached_files(generation, {"services/service_commander_v1.0.0.yaml": b"service"})
    extra_path = generation / extra
    if extra_path.suffix == ".yaml":
        extra_path.write_bytes(b"unselected")
    else:
        extra_path.mkdir(parents=True)
    with pytest.raises(ValueError, match="unselected"):
        _verify_cached_files(generation, {"services/service_commander_v1.0.0.yaml": b"service"})
