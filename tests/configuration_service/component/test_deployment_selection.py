"""Deployment 发布拒绝缺失闭包，并保留历史 revision 的精确选择。"""

from __future__ import annotations

import pytest
import yaml

from configuration_service.application.service import ConfigurationApplication, DocumentInput
from configuration_service.domain.models import ConfigIdentity, RepositoryConflictError
from tests.configuration_service.conftest import publish_documents

pytestmark = pytest.mark.component


def _document(kind: str, identity: str, version: str, **fields: object) -> DocumentInput:
    directory = {"deployment": "deployments", "service": "services", "site": "instances"}[kind]
    filename = f"{kind}_{identity}_v{version}.yaml"
    return DocumentInput(
        identity=ConfigIdentity(kind, identity),
        file_name=filename,
        relative_path=f"{directory}/{filename}",
        yaml_bytes=yaml.safe_dump(
            {"schema_version": 1, "file_version": version, **fields}, sort_keys=False
        ).encode(),
    )


def _documents(version: str = "1.0.0", *, missing: str = "") -> tuple[DocumentInput, ...]:
    deployment = _document(
        "deployment",
        "prod",
        version,
        deployment_id="prod",
        services={"storage-main": {"config_id": "storage-main", "file_version": version}},
        instances={} if missing == "selection" else {"site": {"farm": {"file_version": version}}},
    )
    service = _document(
        "service",
        "storage-main",
        version,
        service_id="storage-main",
        config_sources={"instances": {"site": "farm"}},
    )
    site = _document("site", "farm", version, site_id="farm")
    return tuple(
        item for item in (deployment, service, site) if item.identity.config_type != missing
    )


@pytest.mark.parametrize("missing", ["service", "site", "selection"])
def test_incomplete_deployment_cannot_be_staged(
    app: ConfigurationApplication, missing: str
) -> None:
    with pytest.raises(
        RepositoryConflictError, match="absent staged version|required Service Instance"
    ):
        app.create_candidate(
            actor_id="publisher",
            request_id="invalid",
            expected_revision=0,
            documents=_documents(missing=missing),
            validate_only=False,
        )
    assert app.repository.current_revision() == 0


def test_historical_service_bundle_keeps_exact_selected_versions(
    app: ConfigurationApplication,
) -> None:
    first, _ = publish_documents(app, _documents(), request_id="first")
    _, fingerprint, before = app.get_service_config_bundle("prod", "storage-main", first)
    second, _ = publish_documents(
        app, _documents("1.0.1"), expected_revision=first, request_id="second"
    )
    revision, old_fingerprint, historical = app.get_service_config_bundle(
        "prod", "storage-main", first
    )
    assert revision == first
    assert old_fingerprint == fingerprint
    assert historical == before
    assert {doc.file_version for doc in historical} == {"1.0.0"}
    revision, current_fingerprint, current = app.get_service_config_bundle("prod", "storage-main")
    assert revision == second
    assert current_fingerprint != fingerprint
    assert {doc.file_version for doc in current} == {"1.0.1"}
