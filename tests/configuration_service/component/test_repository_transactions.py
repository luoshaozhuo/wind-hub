"""Candidate, CAS, immutable-history and dependency-graph behavior."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from configuration_service.application.service import ConfigurationApplication
from configuration_service.domain.models import (
    ConfigIdentity,
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
    item = document_input("site", "alpha", "1.0.0")
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
    changed = document_input("site", "alpha", "1.0.0", name="changed")
    with pytest.raises(IdempotencyConflictError):
        app.create_candidate(
            actor_id="publisher",
            request_id="same",
            expected_revision=0,
            documents=(changed,),
            validate_only=False,
        )


def test_unpublished_candidate_cannot_change_baseline_on_replay(
    app: ConfigurationApplication,
) -> None:
    item = document_input("site", "alpha", "1.0.0")
    app.create_candidate(
        actor_id="publisher",
        request_id="baseline-sensitive",
        expected_revision=0,
        documents=(item,),
        validate_only=False,
    )
    publish_documents(
        app,
        (document_input("site", "other", "1.0.0"),),
        expected_revision=0,
        request_id="advance-baseline",
    )
    with pytest.raises(IdempotencyConflictError, match="another revision"):
        app.create_candidate(
            actor_id="publisher",
            request_id="baseline-sensitive",
            expected_revision=1,
            documents=(item,),
            validate_only=False,
        )


def test_publish_replay_is_stable_and_reused_request_is_rejected(
    app: ConfigurationApplication,
) -> None:
    item = document_input("site", "alpha", "1.0.0")
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
        documents=(document_input("site", "other", "1.0.0"),),
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
            documents=(document_input("site", name, "1.0.0"),),
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
            documents=(document_input("site", name, "1.0.0"),),
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
    item = document_input("site", "alpha", "1.0.0")
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
        document_input("site", "one", "1.0.0"),
        document_input("site", "two", "1.0.0"),
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
    identity = document_input("site", "alpha", "1.0.0").identity
    revision1, (version1,) = publish_documents(
        app,
        (document_input("site", "alpha", "1.0.0", name="v1"),),
        request_id="v1",
    )
    revision2, version_ids = publish_documents(
        app,
        (
            document_input("site", "alpha", "2.0.0", name="v2"),
            document_input("site", "beta", "1.0.0"),
        ),
        expected_revision=revision1,
        request_id="v2",
    )
    assert len(version_ids) == 2
    version2 = app.get_config(identity, revision_id=revision2).version_id
    assert app.get_config(identity, revision_id=revision1).file_version == "1.0.0"
    assert app.get_config(identity, revision_id=revision2).file_version == "2.0.0"
    assert app.get_config(identity, version_id=version1).file_version == "1.0.0"
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
        "site",
        "consumer",
        "1.0.0",
        config_sources=[reference("units", "standard", "1.0.0")],
    )
    candidate, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="consumer-create",
        expected_revision=revision3,
        documents=(units, consumer),
        validate_only=False,
    )
    with pytest.raises(RepositoryConflictError, match="deprecated (version|reference)"):
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
    rev1, (a_version,) = publish_documents(
        app,
        (document_input("site", "a", "1.0.0"),),
        request_id="a",
    )
    rev2, ids = publish_documents(
        app,
        (
            document_input("site", "a", "1.0.0"),
            document_input("site", "b", "1.0.0", config_sources=[reference("site", "a", "1.0.0")]),
            document_input("site", "c", "1.0.0", config_sources=[reference("site", "a", "1.0.0")]),
        ),
        expected_revision=rev1,
        request_id="bc",
    )
    rev3, _ = publish_documents(
        app,
        (
            document_input("site", "a", "1.0.0"),
            document_input("site", "b", "1.0.0", config_sources=[reference("site", "a", "1.0.0")]),
            document_input("site", "c", "1.0.0", config_sources=[reference("site", "a", "1.0.0")]),
            document_input(
                "site",
                "d",
                "1.0.0",
                config_sources=[reference("site", "b", "1.0.0"), reference("site", "c", "1.0.0")],
            ),
        ),
        expected_revision=rev2,
        request_id="d",
    )
    d_version = app.repository.resolve_version(ConfigIdentity("site", "d"), "1.0.0")
    bundle = app.repository.get_bundle_versions(
        (document_input("site", "d", "1.0.0").identity,),
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
            (document_input("site", "d", "1.0.0").identity,),
            rev3,
            max_versions=3,
        )


def test_recommend_rollback_requires_current_dependency_closure(
    app: ConfigurationApplication,
) -> None:
    root_v1 = document_input(
        "site",
        "root",
        "1.0.0",
        config_sources=[reference("site", "dependency", "1.0.0")],
    )
    dependency = document_input("site", "dependency", "1.0.0")
    rev1, _ = publish_documents(app, (root_v1, dependency), request_id="closure-v1")
    root_v2 = document_input("site", "root", "2.0.0")
    rev2, _ = publish_documents(app, (root_v2,), expected_revision=rev1, request_id="closure-v2")
    root_v1_id = app.repository.resolve_version(root_v1.identity, "1.0.0")
    with pytest.raises(RepositoryConflictError, match="dependency closure"):
        app.repository.set_recommended_version(
            actor_id="publisher",
            request_id="rollback-without-closure",
            identity=root_v1.identity,
            version_id=root_v1_id,
            expected_revision=rev2,
        )


def test_identical_full_set_is_noop_and_original_request_can_be_replayed(
    app: ConfigurationApplication,
) -> None:
    item = document_input("site", "alpha", "1.0.0")
    candidate, _ = app.create_candidate(
        actor_id="publisher",
        request_id="stable-candidate",
        expected_revision=0,
        documents=(item,),
        validate_only=False,
    )
    first = app.publish_candidate(
        actor_id="publisher",
        request_id="stable-publish",
        candidate_id=candidate,
        expected_revision=0,
    )
    replay_candidate, _ = app.create_candidate(
        actor_id="publisher",
        request_id="stable-candidate",
        expected_revision=first.revision_id,
        documents=(item,),
        validate_only=False,
    )
    assert replay_candidate == candidate
    replay = app.publish_candidate(
        actor_id="publisher",
        request_id="stable-publish",
        candidate_id=replay_candidate,
        expected_revision=0,
    )
    assert replay.revision_id == first.revision_id
    assert replay.replayed is True

    unchanged_candidate, _ = app.create_candidate(
        actor_id="publisher",
        request_id="unchanged-candidate",
        expected_revision=first.revision_id,
        documents=(item,),
        validate_only=False,
    )
    unchanged = app.publish_candidate(
        actor_id="publisher",
        request_id="unchanged-publish",
        candidate_id=unchanged_candidate,
        expected_revision=first.revision_id,
    )
    assert unchanged.revision_id == first.revision_id
    assert unchanged.replayed is True
    assert app.repository.current_revision() == first.revision_id


def test_cyclic_candidate_is_rejected_atomically(app: ConfigurationApplication) -> None:
    documents = (
        document_input("site", "a", "1.0.0", config_sources=[reference("site", "b", "1.0.0")]),
        document_input("site", "b", "1.0.0", config_sources=[reference("site", "a", "1.0.0")]),
    )
    candidate, prepared = app.create_candidate(
        actor_id="publisher",
        request_id="cycle-create",
        expected_revision=0,
        documents=documents,
        validate_only=False,
    )
    with pytest.raises(RepositoryConflictError, match="cyclic"):
        app.repository.publish_candidate(
            actor_id="publisher",
            request_id="cycle-publish",
            candidate_id=candidate,
            expected_revision=0,
            versions=app._compile_versions(prepared, 0),
        )
    assert app.repository.current_revision() == 0


def test_restart_preserves_versions_and_releases_ownership(
    repository: SQLiteRepository, app: ConfigurationApplication, tmp_path
) -> None:
    item = document_input("site", "alpha", "1.0.0")
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
