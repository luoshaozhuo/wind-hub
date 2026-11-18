"""Full-directory publication manifest invariants."""

from __future__ import annotations

import pytest

from configuration_service.application.directory_manifest import (
    DirectoryFile,
    build_directory_manifest,
)


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
    ("../a.yaml", "a/../b.yaml", "./a.yaml", "/root/a.yaml", "a\\b.yaml",
     "a//b.yaml", "a.yaml/", "a.yml", ""),
)
def test_rejects_unsafe_manifest_paths(path: str) -> None:
    with pytest.raises(ValueError, match="relative_path"):
        _file(path)


def test_rejects_casefold_duplicate_and_empty_publication() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        build_directory_manifest(
            (_file("a.yaml"), _file("A.yaml")), max_documents=2, max_bytes=100
        )
    with pytest.raises(ValueError, match="empty"):
        build_directory_manifest((), max_documents=2, max_bytes=100)


def test_rejects_limits_and_non_utf8_content() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        _file("a.yaml", b"\xff")
    with pytest.raises(ValueError, match="byte limit"):
        build_directory_manifest(
            (_file("a.yaml", b"a" * 100),), max_documents=1, max_bytes=99
        )
    with pytest.raises(ValueError, match="document count"):
        build_directory_manifest(
            (_file("a.yaml"), _file("b.yaml")), max_documents=1, max_bytes=100
        )
