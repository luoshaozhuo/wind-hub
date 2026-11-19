"""Local safety and atomicity guards for the CommonService snapshot commands.

These exercise the pure filesystem/validation layer of ``cli.snapshot_commands``
without any gRPC server, so symlink races, capacity bounds and atomic export
behaviour are deterministic and independent of middleware availability.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import typer

from cli.configuration_commands import get as config_get
from cli.snapshot_commands import (
    _snapshot_files,
    _verify_snapshot,
    _write_snapshot,
    get_snapshot,
)
from contracts import common_pb2


def _file(name: str, content: bytes) -> common_pb2.SnapshotFile:
    return common_pb2.SnapshotFile(
        relative_path=name,
        content=content,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def test_snapshot_files_happy_path_is_sorted_and_hash_stamped(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    (tmp_path / "a.yaml").write_bytes(b"one: 1\n")
    (tmp_path / "b" / "c.yaml").write_bytes(b"two: 2\n")
    files = _snapshot_files(tmp_path)
    assert [item.relative_path for item in files] == ["a.yaml", "b/c.yaml"]
    for item in files:
        assert item.size_bytes == len(item.content)
        assert len(item.sha256) == 64


def test_snapshot_files_reject_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.yaml").write_bytes(b"x: 1\n")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        _snapshot_files(link)


def test_snapshot_files_reject_nested_symlink(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_bytes(b"x: 1\n")
    target = tmp_path / "target.yaml"
    target.write_bytes(b"y: 2\n")
    (tmp_path / "evil.yaml").symlink_to(target)
    with pytest.raises(ValueError, match="symbolic links"):
        _snapshot_files(tmp_path)


def test_snapshot_files_reject_non_yaml_and_empty_tree(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("nope", encoding="utf-8")
    with pytest.raises(ValueError, match="only .yaml"):
        _snapshot_files(tmp_path)
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="1..1000"):
        _snapshot_files(empty)


def test_snapshot_files_reject_casefold_duplicate(tmp_path: Path) -> None:
    # On a case-sensitive filesystem these are distinct files, but the
    # repository treats casefold-equal relative paths as the same entry.
    (tmp_path / "a.yaml").write_bytes(b"x: 1\n")
    (tmp_path / "A.yaml").write_bytes(b"x: 2\n")
    with pytest.raises(ValueError, match="duplicate"):
        _snapshot_files(tmp_path)


def test_snapshot_files_enforce_total_byte_capacity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "a.yaml").write_bytes(b"x" * 10)
    monkeypatch.setattr("cli.snapshot_commands._MAX_BYTES", 5)
    with pytest.raises(ValueError, match="exceeds"):
        _snapshot_files(tmp_path)


def test_verify_snapshot_rejects_checksum_and_size_mismatch() -> None:
    good = _file("a.yaml", b"abc")
    bad_hash = common_pb2.SnapshotFile(
        relative_path="a.yaml", content=b"abc", size_bytes=3, sha256="0" * 64
    )
    with pytest.raises(ValueError, match="SHA-256"):
        _verify_snapshot([bad_hash])
    bad_size = common_pb2.SnapshotFile(
        relative_path="a.yaml",
        content=b"abc",
        size_bytes=99,
        sha256=good.sha256,
    )
    with pytest.raises(ValueError, match="size"):
        _verify_snapshot([bad_size])
    assert _verify_snapshot([good]) == [("a.yaml", b"abc")]


def test_write_snapshot_is_atomic_and_rejects_existing_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "out"
    _write_snapshot(destination, [_file("conf/a.yaml", b"services: []\n")])
    written = destination / "conf" / "a.yaml"
    assert written.read_bytes() == b"services: []\n"
    assert written.stat().st_mode & 0o777 == 0o600
    assert destination.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError, match="must not exist"):
        _write_snapshot(destination, [_file("conf/a.yaml", b"services: []\n")])


def test_write_snapshot_leaves_no_partial_output_on_verify_failure(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "never"
    bad = common_pb2.SnapshotFile(
        relative_path="a.yaml", content=b"abc", size_bytes=3, sha256="0" * 64
    )
    with pytest.raises(ValueError, match="SHA-256"):
        _write_snapshot(destination, [bad])
    assert not destination.exists()
    assert not list(tmp_path.glob(".snapshot-*"))


def test_get_snapshot_scope_arguments_are_validated() -> None:
    with pytest.raises(typer.BadParameter, match="disk, runtime or candidate"):
        get_snapshot("gateway", "frobnicate", None)
    with pytest.raises(typer.BadParameter, match="candidate scope requires"):
        get_snapshot("gateway", "candidate", None)
    with pytest.raises(typer.BadParameter, match="requires candidate scope"):
        get_snapshot("gateway", "disk", None, candidate_id="c1")


def test_dual_mode_get_keeps_repository_and_snapshot_selectors_disjoint(
    tmp_path: Path,
) -> None:
    output = tmp_path / "out.yaml"
    # Snapshot scope cannot carry Repository selectors.
    with pytest.raises(typer.BadParameter, match="cannot use Repository selectors"):
        config_get("gateway", config_id="gateway", scope="disk")
    with pytest.raises(typer.BadParameter, match="cannot use Repository selectors"):
        config_get("gateway", output=output, scope="disk", version_id="v1")
    with pytest.raises(typer.BadParameter, match="cannot use Repository selectors"):
        config_get("gateway", output=output, scope="disk", revision_id=2)
    # candidate-id is only legal with the candidate scope.
    with pytest.raises(typer.BadParameter, match="requires --scope candidate"):
        config_get("task", candidate_id="c1")
    # Repository mode requires both identity and an output path.
    with pytest.raises(typer.BadParameter, match="requires config_id and output"):
        config_get("task", config_id="cli")
    # Exactly one of version_id / revision_id must be supplied.
    with pytest.raises(typer.BadParameter, match="exactly one"):
        config_get("task", config_id="cli", output=output, version_id="v1", revision_id=2)
    with pytest.raises(typer.BadParameter, match="exactly one"):
        config_get("task", config_id="cli", output=output)
