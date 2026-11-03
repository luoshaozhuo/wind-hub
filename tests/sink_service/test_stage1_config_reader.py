"""Reader polling: failed application never advances the effective revision."""

from __future__ import annotations

from pathlib import Path

import pytest

from sink_service.application.config import validate_snapshot
from sink_service.domain.config import ValidatedSnapshot
from sink_service.infrastructure.config_repository import GenerationStore
from sink_service.infrastructure.config_sync import ConfigReader
from tests.sink_service._stage1_fixtures import snapshot_docs, snapshot_files, write_tree

pytestmark = [pytest.mark.component, pytest.mark.reliability]


async def _commit(tmp_path: Path, docs: dict, *, old: ValidatedSnapshot | None) -> object:
    snapshot = validate_snapshot(snapshot_files(docs))
    store = GenerationStore(tmp_path)
    if old is None:
        write_tree(tmp_path, docs)
        store.commit(snapshot, "", old=None)
    else:
        store.commit(snapshot, old.revision, old=old)
    return snapshot


async def test_reader_applies_committed_revision(tmp_path: Path) -> None:
    seen: list[str] = []
    reader = ConfigReader(
        tmp_path,
        "services/sink-realtime.yaml",
        lambda snapshot: _record(seen, snapshot),
        poll_interval_ms=10,
    )
    first = await _commit(tmp_path, snapshot_docs(config_root=str(tmp_path)), old=None)
    assert await reader.refresh() is True
    assert reader.last_revision == first.revision
    assert reader.last_error == ""
    assert await reader.refresh() is False  # no new generation


async def test_failed_apply_keeps_effective_revision_and_polls_again(tmp_path: Path) -> None:
    calls = {"fail": True, "seen": []}

    async def apply(snapshot: ValidatedSnapshot) -> None:
        calls["seen"].append(snapshot.revision)
        if calls["fail"]:
            raise RuntimeError("delivery not converged")

    reader = ConfigReader(
        tmp_path,
        "services/sink-realtime.yaml",
        apply,
        poll_interval_ms=10,
    )
    first = await _commit(tmp_path, snapshot_docs(config_root=str(tmp_path)), old=None)
    assert await reader.refresh() is False
    assert reader.last_revision == ""
    assert "not converged" in reader.last_error
    assert calls["seen"] == [first.revision]

    calls["fail"] = False
    assert await reader.refresh() is True
    assert reader.last_revision == first.revision
    assert reader.last_error == ""


async def test_reader_eventually_observes_second_generation(tmp_path: Path) -> None:
    seen: list[str] = []
    reader = ConfigReader(
        tmp_path,
        "services/sink-realtime.yaml",
        lambda snapshot: _record(seen, snapshot),
        poll_interval_ms=10,
    )
    first = await _commit(tmp_path, snapshot_docs(config_root=str(tmp_path)), old=None)
    await reader.refresh()

    docs = snapshot_docs(config_root=str(tmp_path))
    docs["instances/deliveries.yaml"]["deliveries"]["ems_power"]["desired_state"] = "STOP"
    second = await _commit(tmp_path, docs, old=first)
    assert await reader.refresh() is True
    assert seen == [first.revision, second.revision]


async def _record(seen: list[str], snapshot: ValidatedSnapshot) -> None:
    seen.append(snapshot.revision)
