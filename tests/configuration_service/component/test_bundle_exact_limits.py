"""Application-level byte and closure quotas include their exact boundaries."""

from __future__ import annotations

from dataclasses import replace

import pytest

from configuration_service.application.service import ConfigurationApplication
from configuration_service.domain.models import ConfigIdentity, RepositoryCapacityExceededError
from tests.configuration_service.conftest import document_input, publish_documents

pytestmark = pytest.mark.component


def reference(config_id: str) -> dict[str, str]:
    return {"config_type": "site", "config_id": config_id, "file_version": "1.0.0"}


def limited_app(
    app: ConfigurationApplication, *, max_bundle_bytes: int, max_dependencies: int = 1000
) -> ConfigurationApplication:
    return ConfigurationApplication(
        app.repository,
        limits=replace(
            app.limits,
            max_bundle_bytes=max_bundle_bytes,
            max_dependencies=max_dependencies,
        ),
        backups=app.backups,
        own_service_id=app.own_service_id,
        codec=app.codec,
    )


def test_exact_single_document_and_bundle_limits_succeed(
    app: ConfigurationApplication,
) -> None:
    publish_documents(app, (document_input("site", "root", "1.0.0"),), request_id="exact-root")
    revision, _ = publish_documents(
        app,
        (document_input("site", "dependent", "1.0.0", config_sources=[reference("root")]),),
        expected_revision=1,
        request_id="exact-dependent",
    )
    root = app.get_config(ConfigIdentity("site", "root"), revision_id=revision)
    dependent = app.get_config(ConfigIdentity("site", "dependent"), revision_id=revision)

    single_app = limited_app(app, max_bundle_bytes=len(root.yaml_bytes))
    exact_single = single_app.get_config(ConfigIdentity("site", "root"), revision_id=revision)
    assert len(exact_single.yaml_bytes) == single_app.limits.max_bundle_bytes

    exact_bytes = len(root.yaml_bytes) + len(dependent.yaml_bytes)
    exact_app = limited_app(app, max_bundle_bytes=exact_bytes, max_dependencies=2)
    bundle = exact_app.get_bundle(
        (ConfigIdentity("site", "dependent"),), revision, include_dependencies=True
    )
    assert len(bundle) == 2
    assert sum(len(item.yaml_bytes) for item in bundle) == exact_bytes

    overflowing_app = limited_app(exact_app, max_bundle_bytes=exact_bytes - 1, max_dependencies=2)
    with pytest.raises(RepositoryCapacityExceededError, match="bundle exceeds"):
        overflowing_app.get_bundle(
            (ConfigIdentity("site", "dependent"),), revision, include_dependencies=True
        )
