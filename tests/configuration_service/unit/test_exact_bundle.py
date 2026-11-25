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
    repo.get_version_closure.assert_not_called()


def test_exact_bundle_uses_pinned_version_not_current_recommendation() -> None:
    app, repo = _application()
    identity = ConfigIdentity("device", "wtg001")
    repo.get_version_identity.return_value = identity
    repo.get_version_closure.return_value = ()
    assert app.get_exact_bundle(((identity, "historical-sha"),)) == ()
    repo.get_version_closure.assert_called_once_with(
        ["historical-sha"], max_versions=16
    )
    repo.get_bundle_versions.assert_not_called()


@pytest.mark.parametrize("roots", [(), ((ConfigIdentity("device", "a"), ""),)])
def test_exact_bundle_rejects_missing_roots_or_version(roots: tuple[object, ...]) -> None:
    app, _ = _application()
    with pytest.raises(ValueError):
        app.get_exact_bundle(roots)  # type: ignore[arg-type]
