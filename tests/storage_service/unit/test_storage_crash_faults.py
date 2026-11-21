"""Fault injection for Storage snapshot durability and SQLite atomicity."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from storage_service.application.config import load_storage_config
from storage_service.infrastructure.adapters.outbound.configuration_grpc import StorageBundle
from storage_service.infrastructure.adapters.outbound.configuration_snapshot import (
    StorageBundleSnapshot,
)
from storage_service.infrastructure.adapters.outbound.metadata_store import MetadataStore
from tests.storage_service.unit.test_device_binding import TABLE, _binding

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[3]


def _bundle(revision: int) -> StorageBundle:
    config = load_storage_config(
        (ROOT / "configs/services/service_storage-main_v1.1.0.yaml").read_bytes()
    )
    return StorageBundle(
        configuration=config,
        revision_id=revision,
        deployment_id="prod-a",
        target_revision=f"{revision:064x}",
        point_table_versions={TABLE: ("shared", "pin-1.0.0")},
        business_points_pin=None,
        device_bindings=(_binding(),),
    )


def test_replace_failure_keeps_previous_accepted_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from storage_service.infrastructure.adapters.outbound import configuration_snapshot as module

    snapshots = StorageBundleSnapshot(tmp_path / "snapshots")
    previous, next_bundle = _bundle(1), _bundle(2)
    snapshots.store(previous)

    def fail_replace(source: str, destination: str | os.PathLike[str]) -> None:
        raise OSError("injected os.replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected"):
            snapshots.store(next_bundle)

    assert snapshots.load() == previous
    assert not list((tmp_path / "snapshots").glob(".bundle-*"))


def test_fsync_failure_before_replace_preserves_active_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from storage_service.infrastructure.adapters.outbound import configuration_snapshot as module

    snapshots = StorageBundleSnapshot(tmp_path / "snapshots")
    previous, next_bundle = _bundle(1), _bundle(2)
    snapshots.store(previous)

    def fail_fsync(fd: int) -> None:
        raise OSError("injected staging fsync failure")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fsync", fail_fsync)
        with pytest.raises(OSError, match="staging fsync"):
            snapshots.store(next_bundle)

    assert snapshots.load() == previous
    assert not list((tmp_path / "snapshots").glob(".bundle-*"))


def test_directory_fsync_failure_never_exposes_partial_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from storage_service.infrastructure.adapters.outbound import configuration_snapshot as module

    snapshots = StorageBundleSnapshot(tmp_path / "snapshots")
    snapshots.store(_bundle(1))
    real_fsync = os.fsync
    calls = 0

    def inject_directory_failure(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected directory fsync failure")
        real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fsync", inject_directory_failure)
        with pytest.raises(OSError, match="directory fsync"):
            snapshots.store(_bundle(2))

    # After os.replace but before directory fsync, the name may be either
    # generation following a real crash; either must be a complete valid JSON.
    assert snapshots.load().revision_id in (1, 2)
    assert not list((tmp_path / "snapshots").glob(".bundle-*"))


def test_sqlite_device_binding_failure_rolls_back_semantic_pin(
    tmp_path: Path,
) -> None:
    metadata_path = tmp_path / "storage-metadata.sqlite3"
    with MetadataStore(metadata_path) as metadata:
        with sqlite3.connect(metadata_path) as external:
            external.executescript(
                """
                CREATE TRIGGER abort_historical_binding
                BEFORE INSERT ON device_binding_pins
                BEGIN
                    SELECT RAISE(ABORT, 'injected device binding write failure');
                END;
                """
            )
        with pytest.raises(sqlite3.IntegrityError, match="injected"):
            metadata.register_semantic_pins(
                point_table_versions={TABLE: ("shared", "pin-1.0.0")},
                business_points_pin=None,
                device_bindings=(_binding(),),
            )
        assert metadata.list_semantic_pins() == {}
        assert metadata.list_device_bindings() == ()

    with sqlite3.connect(metadata_path) as external:
        external.execute("DROP TRIGGER abort_historical_binding")
    with MetadataStore(metadata_path) as metadata:
        metadata.register_semantic_pins(
            point_table_versions={TABLE: ("shared", "pin-1.0.0")},
            business_points_pin=None,
            device_bindings=(_binding(),),
        )
        assert metadata.list_device_bindings() == (_binding(),)
