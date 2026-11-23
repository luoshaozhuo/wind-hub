"""Candidate, CAS, immutable-history and dependency-graph behavior."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from configuration_service.application.service import ConfigurationApplication
from configuration_service.domain.models import (
    IdempotencyConflictError,
    RepositoryCapacityExceededError,
    RepositoryConflictError,
    RevisionConflictError,
    RowProjection,
)
from configuration_service.infrastructure.adapters.outbound.sqlite.repository import (
    SQLiteRepository,
)
from tests.configuration_service.conftest import document_input, publish_documents

pytestmark = pytest.mark.component


def reference(config_type: str, config_id: str, file_version: str) -> dict[str, str]:
    return {
        "config_type": config_type,
        "config_id": config_id,
        "file_version": file_version,
    }


def test_candidate_idempotency_and_request_payload_conflict(
    app: ConfigurationApplication,
) -> None:
    item = document_input("collector_tasks", "alpha", "1.0.0")
    first, _ = app.create_candidate(
        actor_id="publisher",
        request_id="same",
        expected_revision=0,
        documents=(item,),
        validate_only=False,
    )
    replay, _ = app.create_candidate(
        actor_id="publisher",
        request_id="same",
        expected_revision=0,
        documents=(item,),
        validate_only=False,
    )
    assert replay == first
    changed = document_input(
        "collector_tasks",
        "alpha",
        "1.0.0",
        tasks={"sample": {"group": "changed", "desired_state": "STOP"}},
    )
    with pytest.raises(IdempotencyConflictError):
        app.create_candidate(
            actor_id="publisher",
            request_id="same",
            expected_revision=0,
            documents=(changed,),
            validate_only=False,
        )


def test_publish_replay_is_stable_and_reused_request_is_rejected(
    app: ConfigurationApplication,
) -> None:
    item = document_input("collector_tasks", "alpha", "1.0.0")
    candidate, _ = app.create_candidate(
        actor_id="publisher",
        request_id="create",
        expected_revision=0,
        documents=(item,),
        validate_only=False,
    )
    first = app.publish_candidate(
        actor_id="publisher",
        request_id="publish",
        candidate_id=candidate,
        expected_revision=0,
    )
    replay = app.publish_candidate(
        actor_id="publisher",
        request_id="publish",
        candidate_id=candidate,
        expected_revision=0,
    )
    assert replay.replayed is True
    assert replay.version_ids == first.version_ids
    assert app.repository.get_revision_timestamp(
        replay.revision_id
    ) == app.repository.get_revision_timestamp(first.revision_id)
    other, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="create-other",
        expected_revision=1,
        documents=(document_input("collector_tasks", "other", "1.0.0"),),
        validate_only=False,
    )
    with pytest.raises(IdempotencyConflictError):
        app.repository.publish_candidate(
            actor_id="publisher",
            request_id="publish",
            candidate_id=other,
            expected_revision=1,
            versions=app._compile_versions(prepared, 1),
        )


def test_only_one_cas_candidate_can_publish(app: ConfigurationApplication) -> None:
    candidates = []
    for name in ("one", "two"):
        candidate, _ = app.create_candidate(
            actor_id="publisher",
            request_id=f"create-{name}",
            expected_revision=0,
            documents=(document_input("collector_tasks", name, "1.0.0"),),
            validate_only=False,
        )
        candidates.append(candidate)
    first = app.publish_candidate(
        actor_id="publisher",
        request_id="publish-one",
        candidate_id=candidates[0],
        expected_revision=0,
    )
    assert first.revision_id == 1
    with pytest.raises(RevisionConflictError):
        app.publish_candidate(
            actor_id="publisher",
            request_id="publish-two",
            candidate_id=candidates[1],
            expected_revision=0,
        )


def test_concurrent_publish_has_one_cas_winner(app: ConfigurationApplication) -> None:
    candidates = []
    for name in ("one", "two"):
        candidate, _ = app.create_candidate(
            actor_id="publisher",
            request_id=f"create-{name}",
            expected_revision=0,
            documents=(document_input("collector_tasks", name, "1.0.0"),),
            validate_only=False,
        )
        candidates.append(candidate)

    def publish(item: tuple[int, str]) -> str:
        index, candidate = item
        try:
            app.publish_candidate(
                actor_id="publisher",
                request_id=f"publish-{index}",
                candidate_id=candidate,
                expected_revision=0,
            )
        except RevisionConflictError:
            return "CONFLICT"
        return "COMMITTED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = sorted(pool.map(publish, enumerate(candidates)))
    assert results == ["COMMITTED", "CONFLICT"]
    assert app.repository.current_revision() == 1


def test_expired_candidate_cannot_publish(app: ConfigurationApplication) -> None:
    item = document_input("collector_tasks", "alpha", "1.0.0")
    candidate, _ = app.create_candidate(
        actor_id="publisher",
        request_id="create",
        expected_revision=0,
        documents=(item,),
        validate_only=False,
    )
    with app.repository.write() as connection:
        connection.execute(
            "UPDATE candidates SET expires_at = '2000-01-01T00:00:00+00:00' "
            "WHERE candidate_id = ?",
            (candidate,),
        )
    with pytest.raises(RepositoryConflictError, match="expired"):
        app.publish_candidate(
            actor_id="publisher",
            request_id="publish",
            candidate_id=candidate,
            expected_revision=0,
        )


def test_failed_projection_rolls_back_entire_publication(
    app: ConfigurationApplication,
) -> None:
    documents = (
        document_input("collector_tasks", "one", "1.0.0"),
        document_input("collector_tasks", "two", "1.0.0"),
    )
    candidate, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="create",
        expected_revision=0,
        documents=documents,
        validate_only=False,
    )
    versions = list(app._compile_versions(prepared, 0))
    versions[1] = replace(
        versions[1],
        projections=(*versions[1].projections, RowProjection("unsupported", {})),
    )
    with pytest.raises(ValueError, match="unsupported projection"):
        app.repository.publish_candidate(
            actor_id="publisher",
            request_id="publish",
            candidate_id=candidate,
            expected_revision=0,
            versions=versions,
        )
    assert app.repository.current_revision() == 0
    with app.repository.read() as connection:
        assert connection.execute("SELECT COUNT(*) FROM config_versions").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT status FROM candidates WHERE candidate_id = ?",
                (candidate,),
            ).fetchone()[0]
            == "VALID"
        )


def test_full_revision_snapshot_and_recommendation_keep_history_immutable(
    app: ConfigurationApplication,
) -> None:
    identity = document_input("collector_tasks", "alpha", "1.0.0").identity
    alpha_v1 = document_input(
        "collector_tasks",
        "alpha",
        "1.0.0",
        tasks={"sample": {"group": "v1", "desired_state": "STOP"}},
    )
    alpha_v2 = document_input(
        "collector_tasks",
        "alpha",
        "2.0.0",
        tasks={"sample": {"group": "v2", "desired_state": "STOP"}},
    )
    beta = document_input("collector_tasks", "beta", "1.0.0")
    revision1, (version1,) = publish_documents(app, (alpha_v1,), request_id="v1")
    # Both immutable alpha versions coexist in the complete root; the highest
    # version becomes the recommended one.
    revision2, version_ids = publish_documents(
        app,
        (alpha_v1, alpha_v2, beta),
        expected_revision=revision1,
        request_id="v2",
    )
    assert len(version_ids) == 3
    version2 = app.get_config(identity, revision_id=revision2).version_id
    assert app.get_config(identity, revision_id=revision1).file_version == "1.0.0"
    assert app.get_config(identity, revision_id=revision2).file_version == "2.0.0"
    assert app.get_config(identity, version_id=version1).file_version == "1.0.0"
    # Recommendation only accepts a version present in the current complete
    # directory; v1 is still part of the root, so the explicit switch is legal.
    revision3, previous = app.repository.set_recommended_version(
        actor_id="publisher",
        request_id="recommend",
        identity=identity,
        version_id=version1,
        expected_revision=revision2,
    )
    assert previous == version2
    assert app.get_config(identity, revision_id=revision3).version_id == version1
    assert app.get_config(identity, revision_id=revision2).version_id == version2


def test_deprecated_dependency_cannot_be_newly_bound(
    app: ConfigurationApplication,
) -> None:
    units = document_input(
        "units",
        "standard",
        "1.0.0",
        quantities={"power": {"base_unit": "watt"}},
        units={"watt": {"quantity": "power", "symbol": "W"}},
    )
    revision1, (unit_version,) = publish_documents(app, (units,), request_id="units")
    revision2, (recommended_version,) = publish_documents(
        app,
        (
            document_input(
                "units",
                "standard",
                "2.0.0",
                quantities={"power": {"base_unit": "watt"}},
                units={"watt": {"quantity": "power", "symbol": "W"}},
            ),
        ),
        expected_revision=revision1,
        request_id="units-v2",
    )
    assert recommended_version != unit_version
    revision3, _ = app.repository.deprecate_version(
        actor_id="admin",
        request_id="deprecate",
        version_id=unit_version,
        reason="retired",
        expected_revision=revision2,
    )
    consumer = document_input(
        "collector_tasks",
        "consumer",
        "1.0.0",
        config_sources=[reference("units", "standard", "1.0.0")],
    )
    # Full-directory snapshots restate the pinned closure; the commit guard
    # refuses to reactivate a deprecated immutable version inside a new root.
    deprecated_units = document_input(
        "units",
        "standard",
        "1.0.0",
        quantities={"power": {"base_unit": "watt"}},
        units={"watt": {"quantity": "power", "symbol": "W"}},
    )
    candidate, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="consumer-create",
        expected_revision=revision3,
        documents=(deprecated_units, consumer),
        validate_only=False,
    )
    with pytest.raises(RepositoryConflictError, match="deprecated"):
        app.repository.publish_candidate(
            actor_id="publisher",
            request_id="consumer-publish",
            candidate_id=candidate,
            expected_revision=revision3,
            versions=app._compile_versions(prepared, revision3),
        )


def test_shared_diamond_closure_is_deduplicated_and_topological(
    app: ConfigurationApplication,
) -> None:
    a_doc = document_input("collector_tasks", "a", "1.0.0")
    rev1, (a_version,) = publish_documents(app, (a_doc,), request_id="a")
    b_doc = document_input(
        "collector_tasks", "b", "1.0.0", config_sources=[reference("collector_tasks", "a", "1.0.0")]
    )
    c_doc = document_input(
        "collector_tasks", "c", "1.0.0", config_sources=[reference("collector_tasks", "a", "1.0.0")]
    )
    # Every publication is a complete root snapshot, so the unchanged closure
    # is restated; immutable versions resolve back to their existing ids.
    rev2, ids = publish_documents(
        app,
        (a_doc, b_doc, c_doc),
        expected_revision=rev1,
        request_id="bc",
    )
    d_doc = document_input(
        "collector_tasks",
        "d",
        "1.0.0",
        config_sources=[
            reference("collector_tasks", "b", "1.0.0"),
            reference("collector_tasks", "c", "1.0.0"),
        ],
    )
    rev3, rev3_ids = publish_documents(
        app,
        (a_doc, b_doc, c_doc, d_doc),
        expected_revision=rev2,
        request_id="d",
    )
    d_version = rev3_ids[-1]
    bundle = app.repository.get_bundle_versions(
        (document_input("collector_tasks", "d", "1.0.0").identity,),
        rev3,
        max_versions=4,
    )
    assert len(bundle) == 4
    ordered = [row["version_id"] for row in bundle]
    assert ordered[-1] == d_version
    assert ordered[0] == a_version
    assert set(ids).issubset(ordered)
    with pytest.raises(RepositoryCapacityExceededError, match="exceeds limit"):
        app.repository.get_bundle_versions(
            (document_input("collector_tasks", "d", "1.0.0").identity,),
            rev3,
            max_versions=3,
        )


def test_cyclic_candidate_is_rejected_atomically(app: ConfigurationApplication) -> None:
    documents = (
        document_input(
            "collector_tasks",
            "a",
            "1.0.0",
            config_sources=[reference("collector_tasks", "b", "1.0.0")],
        ),
        document_input(
            "collector_tasks",
            "b",
            "1.0.0",
            config_sources=[reference("collector_tasks", "a", "1.0.0")],
        ),
    )
    # Cycles are rejected while staging the full directory, before a candidate
    # id is issued or any SQLite write becomes visible.
    with pytest.raises(RepositoryConflictError, match="cyclic"):
        app.create_candidate(
            actor_id="publisher",
            request_id="cycle-create",
            expected_revision=0,
            documents=documents,
            validate_only=False,
        )
    assert app.repository.current_revision() == 0


def test_restart_preserves_versions_and_releases_ownership(
    repository: SQLiteRepository, app: ConfigurationApplication, tmp_path
) -> None:
    item = document_input("collector_tasks", "alpha", "1.0.0")
    _, (version_id,) = publish_documents(app, (item,), request_id="restart")
    app.get_config(item.identity, version_id=version_id)
    repository.close()
    replacement = SQLiteRepository(tmp_path / "repository.sqlite3", application_version="test")
    replacement.initialize()
    try:
        assert (
            replacement.get_version(item.identity, version_id=version_id)["version_id"]
            == version_id
        )
        second_owner = SQLiteRepository(tmp_path / "repository.sqlite3", application_version="test")
        with pytest.raises(RuntimeError, match="already owned"):
            second_owner.initialize()
    finally:
        replacement.close()
