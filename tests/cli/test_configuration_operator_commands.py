"""Configuration operator CLI request and verified-closure contracts."""

import hashlib
from pathlib import Path
from typing import Any

import pytest

from contracts import configuration_repository_pb2 as pb

pytestmark = pytest.mark.contract


def _document() -> pb.ConfigYamlDocument:
    content = b"schema_version: 1\n"
    return pb.ConfigYamlDocument(
        config_id=pb.ConfigId(config_type="service", config_id="cli"),
        version_id="v1",
        file_version="1.0.0",
        relative_path="services/service_cli_v1.0.0.yaml",
        yaml_bytes=content,
        yaml_sha256=hashlib.sha256(content).hexdigest(),
    )


def _manifest(document: pb.ConfigYamlDocument) -> str:
    digest = hashlib.sha256()
    for value in (document.relative_path, document.version_id, document.yaml_sha256):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def test_config_list_forwards_revision_and_page(cli_env: Any) -> None:
    cli_env.server.respond_value(
        cli_env.server.method_path("ConfigurationRepositoryService", "ListConfigs"),
        pb.ListConfigsResponse(),
    )

    result = cli_env.invoke(
        "config",
        "list",
        "--revision-id",
        "7",
        "--page-size",
        "3",
        "--page-token",
        "next",
    )

    assert result.exit_code == 0, result.stdout
    call = cli_env.server.calls[-1]
    assert call.request.HasField("revision_id")
    assert call.request.revision_id == 7
    assert call.request.page_size == 3
    assert call.request.page_token == "next"
    assert any(key == "authorization" for key, _value in call.metadata)


def test_config_versions_forwards_pagination(cli_env: Any) -> None:
    cli_env.server.respond_value(
        cli_env.server.method_path("ConfigurationRepositoryService", "ListConfigVersions"),
        pb.ListConfigVersionsResponse(),
    )

    result = cli_env.invoke(
        "config",
        "versions",
        "service",
        "cli",
        "--include-deprecated",
        "--page-size",
        "2",
        "--page-token",
        "v2",
    )

    assert result.exit_code == 0, result.stdout
    request = cli_env.server.calls[-1].request
    assert request.config_id.config_type == "service"
    assert request.config_id.config_id == "cli"
    assert request.include_deprecated is True
    assert request.page_size == 2
    assert request.page_token == "v2"


def test_bundle_exact_exports_multiple_immutable_roots(cli_env: Any, tmp_path: Path) -> None:
    document = _document()
    cli_env.server.respond_value(
        cli_env.server.method_path("ConfigurationRepositoryService", "GetConfigBundle"),
        pb.GetConfigBundleResponse(
            revision_id=8,
            files=[document],
            manifest_sha256=_manifest(document),
            total_bytes=len(document.yaml_bytes),
        ),
    )
    output = tmp_path / "bundle"

    result = cli_env.invoke(
        "config",
        "bundle-exact",
        str(output),
        "--exact-root",
        "service:cli:v1",
        "--exact-root",
        "device:farm:v2",
    )

    assert result.exit_code == 0, result.stdout
    request = cli_env.server.calls[-1].request
    assert len(request.exact_roots) == 2
    assert not request.roots
    assert not request.HasField("revision_id")
    assert request.include_dependencies is True
    assert output.stat().st_mode & 0o777 == 0o700
    assert (output / document.relative_path).read_bytes() == document.yaml_bytes


def test_service_bundle_reports_selected_target_without_yaml_bytes(cli_env: Any) -> None:
    document = _document()
    cli_env.server.respond_value(
        cli_env.server.method_path("ConfigurationRepositoryService", "GetServiceConfigBundle"),
        pb.GetServiceConfigBundleResponse(
            repository_revision_id=8,
            target_revision="target-sha",
            files=[document],
            manifest_sha256=_manifest(document),
            total_bytes=len(document.yaml_bytes),
        ),
    )

    result = cli_env.invoke("config", "service-bundle", "prod-a", "cli", "--revision-id", "9")

    assert result.exit_code == 0, result.stdout
    request = cli_env.server.calls[-1].request
    assert request.deployment_id == "prod-a"
    assert request.service_id == "cli"
    assert request.revision_id == 9
    data = result.json()["data"]
    assert data["target_revision"] == "target-sha"
    assert data["files"][0]["relative_path"] == document.relative_path
    assert "yaml_bytes" not in data["files"][0]
