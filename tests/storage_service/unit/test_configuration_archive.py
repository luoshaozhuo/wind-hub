"""不可变 Configuration 归档的全历史容量与幂等持久化。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from storage_service.application.ingestion import ConfigHistoryUnavailableError
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ImmutableConfigArchive,
    RepositoryDocument,
)

pytestmark = pytest.mark.unit


def _document(config_id: str, payload: bytes) -> RepositoryDocument:
    return RepositoryDocument(
        config_type="point_table",
        config_id=config_id,
        version_id="version-1",
        revision_id=1,
        yaml_bytes=payload,
        yaml_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _used_bytes(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.rglob("*") if path.is_file())


def test_persist_is_idempotent_and_rejects_new_history_beyond_quota(
    tmp_path: Path,
) -> None:
    first = _document("table-1", b"points: {one: {unit: W}}")
    second = _document("table-2", b"points: {two: {unit: var}}")
    exact = ImmutableConfigArchive(tmp_path)
    exact.persist(first)
    one_size = _used_bytes(tmp_path)

    exact.persist(first)
    assert _used_bytes(tmp_path) == one_size
    assert (
        exact.load(config_type="point_table", config_id="table-1", version_id="version-1") == first
    )

    budget = ImmutableConfigArchive(tmp_path, max_bytes=one_size)
    budget.persist(first)
    assert _used_bytes(tmp_path) == one_size
    with pytest.raises(ConfigHistoryUnavailableError, match="quota exceeded"):
        budget.persist(second)
    assert _used_bytes(tmp_path) == one_size
    with pytest.raises(ConfigHistoryUnavailableError, match="historical configuration unavailable"):
        budget.load(config_type="point_table", config_id="table-2", version_id="version-1")


def test_set_max_bytes_fails_closed_for_existing_archive_and_duplicate_replay(
    tmp_path: Path,
) -> None:
    archive = ImmutableConfigArchive(tmp_path)
    archive.persist(_document("table-1", b"first"))
    archive.persist(_document("table-2", b"second"))
    used = _used_bytes(tmp_path)

    with pytest.raises(ConfigHistoryUnavailableError, match="already exceeds"):
        archive.set_max_bytes(used - 1)
    archive.set_max_bytes(used)
    with pytest.raises(ConfigHistoryUnavailableError, match="quota exceeded"):
        archive.persist(_document("table-3", b"third"))
    assert _used_bytes(tmp_path) == used


def test_partial_entry_consumes_quota_before_completion(tmp_path: Path) -> None:
    first = _document("table-1", b"complete")
    archive = ImmutableConfigArchive(tmp_path)
    archive.persist(first)
    item_dir = next(path for path in tmp_path.iterdir() if path.is_dir())
    (item_dir / "unrelated.evidence").write_bytes(b"still counted")

    with pytest.raises(ConfigHistoryUnavailableError, match="already exceeds"):
        archive.set_max_bytes(_used_bytes(tmp_path) - 1)
    assert (item_dir / "unrelated.evidence").read_bytes() == b"still counted"
