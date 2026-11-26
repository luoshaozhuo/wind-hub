"""Exact historical Configuration roots do not follow mutable recommendations."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from configuration_service.application.service import ConfigurationApplication, DocumentOutput
from configuration_service.domain.models import ConfigIdentity, RepositoryConflictError


def _output(version_id: str, path: str, content: bytes) -> DocumentOutput:
    return DocumentOutput(
        identity=ConfigIdentity("device", "wtg001"),
        version_id=version_id,
        file_version="1.0.0",
        schema_version=1,
        revision_id=0,
        schema_ref="device:1:hash",
        content_sha256="hash",
        yaml_sha256="hash",
        yaml_bytes=content,
        file_name=path,
        relative_path=path,
    )


def _application() -> tuple[ConfigurationApplication, Mock]:
    repo = Mock()
    app = object.__new__(ConfigurationApplication)
    app.repository = repo
    app.limits = SimpleNamespace(max_dependencies=16, max_bundle_bytes=4096)
    return app, repo


def test_exact_bundle_rejects_identity_mismatch_before_loading_closure() -> None:
    app, repo = _application()
    repo.get_version_identity.return_value = ConfigIdentity("device", "another-device")
    with pytest.raises(RepositoryConflictError, match="disagree"):
        app.get_exact_bundle(((ConfigIdentity("device", "wtg001"), "historical-sha"),))
    repo.get_version_closure_snapshot.assert_not_called()


def test_exact_bundle_uses_pinned_version_not_current_recommendation() -> None:
    app, repo = _application()
    identity = ConfigIdentity("device", "wtg001")
    repo.get_version_identity.return_value = identity
    repo.get_version_closure_snapshot.return_value = (7, ({"version_id": "historical-sha"},))
    output = _output("historical-sha", "device.yaml", b"historical-content")
    app._restore = Mock(return_value=output)
    assert app.get_exact_bundle(((identity, "historical-sha"),)) == (output,)
    repo.get_version_closure_snapshot.assert_called_once_with(["historical-sha"], max_versions=16)
    repo.get_bundle_versions.assert_not_called()


@pytest.mark.parametrize(
    "returned",
    [
        (),
        ({"version_id": "different-sha"},),
        ({"version_id": "historical-sha"}, {"version_id": "historical-sha"}),
    ],
)
def test_exact_bundle_rejects_incomplete_or_duplicate_closure(
    returned: tuple[dict[str, str], ...],
) -> None:
    app, repo = _application()
    identity = ConfigIdentity("device", "wtg001")
    repo.get_version_identity.return_value = identity
    repo.get_version_closure_snapshot.return_value = (7, returned)
    with pytest.raises(RepositoryConflictError, match="incomplete or duplicate"):
        app.get_exact_bundle(((identity, "historical-sha"),))


def test_exact_bundle_rejects_duplicate_version_roots() -> None:
    app, repo = _application()
    identity = ConfigIdentity("device", "wtg001")
    repo.get_version_identity.return_value = identity
    with pytest.raises(ValueError, match="distinct version IDs"):
        app.get_exact_bundle(((identity, "same-sha"), (identity, "same-sha")))
    repo.get_version_closure_snapshot.assert_not_called()


def test_exact_bundle_allows_multiple_versions_of_one_identity() -> None:
    app, repo = _application()
    identity = ConfigIdentity("device", "wtg001")
    repo.get_version_identity.return_value = identity
    records = ({"version_id": "sha-a"}, {"version_id": "sha-b"})
    repo.get_version_closure_snapshot.return_value = (7, records)
    outputs = {
        "sha-a": _output("sha-a", "a.yaml", b"a"),
        "sha-b": _output("sha-b", "b.yaml", b"b"),
    }
    app._restore = Mock(side_effect=lambda record, revision_id: outputs[record["version_id"]])
    roots = ((identity, "sha-a"), (identity, "sha-b"))
    assert app.get_exact_bundle(roots) == (outputs["sha-a"], outputs["sha-b"])
    repo.get_version_closure_snapshot.assert_called_once_with(["sha-a", "sha-b"], max_versions=16)


@pytest.mark.parametrize("roots", [(), ((ConfigIdentity("device", "a"), ""),)])
def test_exact_bundle_rejects_missing_roots_or_version(roots: tuple[object, ...]) -> None:
    app, _ = _application()
    with pytest.raises(ValueError):
        app.get_exact_bundle(roots)  # type: ignore[arg-type]
