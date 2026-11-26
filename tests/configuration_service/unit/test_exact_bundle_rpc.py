"""Exact historical Configuration RPC selectors must remain immutable."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from configuration_service.application.service import DocumentOutput
from configuration_service.domain.models import ConfigIdentity, RepositoryConflictError
from configuration_service.infrastructure.adapters.inbound.grpc.repository_server import (
    RepositoryServicer,
)
from contracts import configuration_repository_pb2 as pb


def _invoke(rpc_request: pb.GetConfigBundleRequest, app: Mock) -> pb.GetConfigBundleResponse:
    servicer = SimpleNamespace(app=app, _actor=Mock(return_value="query-caller"))
    return RepositoryServicer.GetConfigBundle.__wrapped__(servicer, rpc_request, Mock())


def _root(*, version: str = "", file_version: str = "", sha: str = "") -> pb.ConfigVersionRef:
    return pb.ConfigVersionRef(
        config_id=pb.ConfigId(config_type="device", config_id="wtg001"),
        version_id=version,
        file_version=file_version,
        content_sha256=sha,
    )


def _document() -> DocumentOutput:
    return DocumentOutput(
        identity=ConfigIdentity("device", "wtg001"),
        version_id="fixed-version",
        file_version="1.2.3",
        schema_version=1,
        revision_id=0,
        schema_ref="device:1:hash",
        content_sha256="fixed-content",
        yaml_sha256="fixed-yaml",
        yaml_bytes=b"sample",
        file_name="device.yaml",
        relative_path="devices/device.yaml",
    )


@pytest.mark.parametrize(
    "rpc_request",
    [
        pb.GetConfigBundleRequest(exact_roots=[_root(version="fixed-version")]),
        pb.GetConfigBundleRequest(
            exact_roots=[_root(version="fixed-version")], include_dependencies=False
        ),
        pb.GetConfigBundleRequest(exact_roots=[_root()], include_dependencies=True),
    ],
)
def test_exact_bundle_requires_explicit_closure_and_selector(
    rpc_request: pb.GetConfigBundleRequest,
) -> None:
    app = Mock()
    with pytest.raises(ValueError):
        _invoke(rpc_request, app)
    app.get_exact_bundle_snapshot.assert_not_called()


def test_exact_bundle_accepts_fixed_file_version_and_validates_digest() -> None:
    app = Mock()
    app.repository.resolve_version.return_value = "fixed-version"
    app.get_exact_bundle_snapshot.return_value = (7, (_document(),))
    response = _invoke(
        pb.GetConfigBundleRequest(
            exact_roots=[_root(file_version="1.2.3", sha="fixed-content")],
            include_dependencies=True,
        ),
        app,
    )
    app.repository.resolve_version.assert_called_once_with(
        ConfigIdentity("device", "wtg001"), "1.2.3"
    )
    app.get_exact_bundle_snapshot.assert_called_once_with(
        ((ConfigIdentity("device", "wtg001"), "fixed-version"),),
        include_dependencies=True,
    )
    assert response.revision_id == 7
    assert len(response.files) == 1
    assert response.files[0].version_id == "fixed-version"


@pytest.mark.parametrize(
    ("version", "sha", "message"),
    [
        ("wrong-version", "", "selectors disagree"),
        ("fixed-version", "wrong-content", "digest mismatch"),
    ],
)
def test_exact_bundle_rejects_inconsistent_pins(
    version: str,
    sha: str,
    message: str,
) -> None:
    app = Mock()
    app.repository.resolve_version.return_value = "fixed-version"
    app.get_exact_bundle_snapshot.return_value = (7, (_document(),))
    with pytest.raises(RepositoryConflictError, match=message):
        _invoke(
            pb.GetConfigBundleRequest(
                exact_roots=[_root(version=version, file_version="1.2.3", sha=sha)],
                include_dependencies=True,
            ),
            app,
        )
