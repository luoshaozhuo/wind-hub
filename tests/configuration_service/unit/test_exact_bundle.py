"""Exact historical Configuration roots do not follow mutable recommendations."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from configuration_service.application.service import ConfigurationApplication
from configuration_service.domain.models import ConfigIdentity, RepositoryConflictError


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
    output = SimpleNamespace(
        relative_path="device.yaml",
        version_id="historical-sha",
        yaml_bytes=b"historical-content",
    )
    app._restore = Mock(return_value=output)
    assert app.get_exact_bundle(((identity, "historical-sha"),)) == (output,)
    repo.get_version_closure_snapshot.assert_called_once_with(
        ["historical-sha"], max_versions=16
    )
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


@pytest.mark.parametrize(
    "roots",
    [
        ((ConfigIdentity("device", "wtg001"), "same-sha"),) * 2,
        (
            (ConfigIdentity("device", "wtg001"), "sha-a"),
            (ConfigIdentity("device", "wtg001"), "sha-b"),
        ),
    ],
)
def test_exact_bundle_rejects_duplicate_roots(
    roots: tuple[tuple[ConfigIdentity, str], ...],
) -> None:
    app, repo = _application()
    with pytest.raises(ValueError, match="distinct versions and identities"):
        app.get_exact_bundle(roots)
    repo.get_version_closure_snapshot.assert_not_called()


@pytest.mark.parametrize("roots", [(), ((ConfigIdentity("device", "a"), ""),)])
def test_exact_bundle_rejects_missing_roots_or_version(roots: tuple[object, ...]) -> None:
    app, _ = _application()
    with pytest.raises(ValueError):
        app.get_exact_bundle(roots)  # type: ignore[arg-type]
