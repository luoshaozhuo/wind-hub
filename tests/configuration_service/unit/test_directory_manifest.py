"""Full-directory publication manifest invariants."""

from __future__ import annotations

from pathlib import Path

import pytest

from configuration_service.application.directory_manifest import (
    DirectoryFile,
    build_directory_manifest,
)
from configuration_service.infrastructure.adapters.outbound.yaml.codec import parse_yaml
from core.configuration.publication_identity import resolve_publication_identity


def _file(path: str, payload: bytes = b"schema_version: 1\n") -> DirectoryFile:
    return DirectoryFile(relative_path=path, yaml_bytes=payload)


def test_manifest_is_order_independent_and_content_bound() -> None:
    a = _file("templates/a.yaml")
    b = _file("instances/b.yaml")
    first = build_directory_manifest((a, b), max_documents=3, max_bytes=100)
    reversed_order = build_directory_manifest((b, a), max_documents=3, max_bytes=100)
    assert first.sha256 == reversed_order.sha256
    assert first.files == reversed_order.files
    changed = build_directory_manifest(
        (a, _file("instances/b.yaml", b"schema_version: 2\n")),
        max_documents=3,
        max_bytes=100,
    )
    assert first.sha256 != changed.sha256


@pytest.mark.parametrize(
    "path",
    (
        "../a.yaml",
        "a/../b.yaml",
        "./a.yaml",
        "/root/a.yaml",
        "a\\b.yaml",
        "a//b.yaml",
        "a.yaml/",
        "a.yml",
        "",
    ),
)
def test_rejects_unsafe_manifest_paths(path: str) -> None:
    with pytest.raises(ValueError, match="relative_path"):
        _file(path)


def test_rejects_casefold_duplicate_and_empty_publication() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        build_directory_manifest((_file("a.yaml"), _file("A.yaml")), max_documents=2, max_bytes=100)
    with pytest.raises(ValueError, match="empty"):
        build_directory_manifest((), max_documents=2, max_bytes=100)


def test_rejects_limits_and_non_utf8_content() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        _file("a.yaml", b"\xff")
    with pytest.raises(ValueError, match="byte limit"):
        build_directory_manifest((_file("a.yaml", b"a" * 100),), max_documents=1, max_bytes=99)
    with pytest.raises(ValueError, match="document count"):
        build_directory_manifest((_file("a.yaml"), _file("b.yaml")), max_documents=1, max_bytes=100)


@pytest.mark.parametrize(
    ("path", "version", "template_id", "expected_type", "expected_id"),
    [
        (
            "templates/point_tables/point_table_modbus_wtg_sim_v1.0.0.yaml",
            "1.0.0",
            "modbus_wtg_sim",
            "point_table",
            "modbus_wtg_sim",
        ),
        (
            "templates/device_models/device_model_wtg_sim_v2.0.0.yaml",
            "2.0.0",
            "wtg_sim",
            "device_model",
            "wtg_sim",
        ),
        ("instances/device_sim_v1.2.0.yaml", "1.2.0", None, "device", "sim"),
        ("templates/business_points_standard_v1.4.0.yaml", "1.4.0", None, "business_points", "standard"),
        ("templates/units_standard_v1.2.0.yaml", "1.2.0", None, "units", "standard"),
        ("services/service_sink-west_v1.2.0.yaml", "1.2.0", None, "service", "sink-west"),
    ],
)
def test_publication_identity_is_derived_from_exact_path(
    path: str,
    version: str,
    template_id: str | None,
    expected_type: str,
    expected_id: str,
) -> None:
    source: dict[str, object] = {"file_version": version}
    if template_id:
        source["template_id"] = template_id
    if expected_type == "service":
        source["service_id"] = expected_id
    identity = resolve_publication_identity(path, source)
    assert (identity.config_type, identity.config_id, identity.file_version) == (
        expected_type,
        expected_id,
        version,
    )


@pytest.mark.parametrize(
    ("path", "source"),
    [
        ("services/service_configuration_v1.0.0.yaml", {"file_version": "1.0.0"}),
        (
            "templates/point_tables/point_table_wtg_v1.0.0.yaml",
            {"file_version": "1.0.0", "template_id": "other"},
        ),
        (
            "templates/point_tables/point_table_wtg_v1.0.0.yaml",
            {"file_version": "1.0.1", "template_id": "wtg"},
        ),
        ("instances/unrecognized.yaml", {"file_version": "1.0.0"}),
    ],
)
def test_publication_rejects_bootstrap_and_ambiguous_identity(
    path: str, source: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        resolve_publication_identity(path, source)


def test_repository_directory_paths_have_unambiguous_identities() -> None:
    root = Path(__file__).resolve().parents[3] / "configs"
    seen: set[tuple[str, str, str]] = set()
    for path in sorted(root.rglob("*.yaml")):
        relative_path = path.relative_to(root).as_posix()
        document = parse_yaml(path.read_bytes(), max_bytes=64 * 1024 * 1024)
        if relative_path.startswith("services/service_configuration_v"):
            with pytest.raises(ValueError, match="bootstrap"):
                resolve_publication_identity(relative_path, document)
            continue
        identity = resolve_publication_identity(relative_path, document)
        key = (identity.config_type, identity.config_id, identity.file_version)
        assert key not in seen, f"duplicate document identity: {relative_path}"
        seen.add(key)
    assert len(seen) >= 40
