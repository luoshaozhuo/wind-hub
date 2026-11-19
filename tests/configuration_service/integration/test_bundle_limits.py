"""Real response-size and dependency-closure quota enforcement."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import grpc
import pytest
import yaml

from contracts import configuration_repository_pb2 as pb
from tests.configuration_service.integration.test_repository_rpc import document
from tests.support.configuration_live import (
    LiveConfiguration,
    reserve_port,
    write_live_configuration,
)

pytestmark = [pytest.mark.integration, pytest.mark.real_service]


@pytest.fixture
def configurable_live(tmp_path: Path) -> Iterator[LiveConfiguration]:
    live = write_live_configuration(tmp_path, reserve_port())
    try:
        yield live
    finally:
        live.cli("service", "stop", "configuration", "--yes", check=False)


def start_with_limits(
    live: LiveConfiguration, *, max_dependencies: int, max_bundle_bytes: int
) -> None:
    settings = yaml.safe_load(live.bootstrap_path.read_bytes())
    settings["limits"]["max_dependencies"] = max_dependencies
    settings["limits"]["max_bundle_bytes"] = max_bundle_bytes
    live.bootstrap_path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    live.cli("service", "start", "configuration", "--yes")


def restart_with_limits(
    live: LiveConfiguration, *, max_dependencies: int, max_bundle_bytes: int
) -> None:
    live.cli("service", "stop", "configuration", "--yes")
    start_with_limits(live, max_dependencies=max_dependencies, max_bundle_bytes=max_bundle_bytes)


async def publish(
    repository: object,
    item: object,
    *,
    request_id: str,
    expected_revision: int,
) -> None:
    candidate = await repository.CreateCandidate(
        pb.CreateCandidateRequest(
            request_id=f"{request_id}-candidate",
            expected_revision_id=expected_revision,
            documents=[item],
        )
    )
    await repository.PublishCandidate(
        pb.PublishCandidateRequest(
            request_id=f"{request_id}-publish",
            candidate_id=candidate.candidate_id,
            expected_revision_id=expected_revision,
        )
    )


async def publish_dependency_pair(repository: object) -> None:
    await publish(repository, document("root", "1.0.0"), request_id="root", expected_revision=0)
    dependent = document("dependent", "1.0.0")
    payload = yaml.safe_load(dependent.yaml_bytes)
    payload["config_sources"] = [
        {"config_type": "site", "config_id": "root", "file_version": "1.0.0"}
    ]
    dependent.yaml_bytes = yaml.safe_dump(payload).encode()
    await publish(repository, dependent, request_id="dependent", expected_revision=1)


def identity(config_id: str) -> pb.ConfigId:
    return pb.ConfigId(config_type="site", config_id=config_id)


@pytest.mark.asyncio
async def test_single_document_export_limit_is_resource_exhausted(
    configurable_live: LiveConfiguration,
) -> None:
    start_with_limits(configurable_live, max_dependencies=100, max_bundle_bytes=1024)
    repository, _common, channel = configurable_live.stubs("deployer")
    try:
        oversized = document("oversized", "1.0.0")
        payload = yaml.safe_load(oversized.yaml_bytes)
        payload["name"] = "x" * 1500
        oversized.yaml_bytes = yaml.safe_dump(payload).encode()
        await publish(repository, oversized, request_id="oversized", expected_revision=0)
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await repository.GetConfig(
                pb.GetConfigRequest(config_id=identity("oversized"), revision_id=1)
            )
        assert rejected.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_dependency_closure_limit_is_resource_exhausted(
    configurable_live: LiveConfiguration,
) -> None:
    start_with_limits(configurable_live, max_dependencies=1, max_bundle_bytes=8 * 1024 * 1024)
    repository, _common, channel = configurable_live.stubs("deployer")
    try:
        await publish_dependency_pair(repository)
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await repository.GetConfigBundle(
                pb.GetConfigBundleRequest(
                    roots=[identity("dependent")], revision_id=2, include_dependencies=True
                )
            )
        assert rejected.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_bundle_total_limit_succeeds_with_headroom_and_rejects_one_extra_byte(
    configurable_live: LiveConfiguration,
) -> None:
    start_with_limits(configurable_live, max_dependencies=100, max_bundle_bytes=8 * 1024 * 1024)
    repository, _common, channel = configurable_live.stubs("deployer")
    try:
        await publish_dependency_pair(repository)
        root = await repository.GetConfig(
            pb.GetConfigRequest(config_id=identity("root"), revision_id=2)
        )
        dependent = await repository.GetConfig(
            pb.GetConfigRequest(config_id=identity("dependent"), revision_id=2)
        )
        exact_bytes = len(root.document.yaml_bytes) + len(dependent.document.yaml_bytes)
        restart_with_limits(
            configurable_live, max_dependencies=2, max_bundle_bytes=exact_bytes + 1024
        )
        bundle = await repository.GetConfigBundle(
            pb.GetConfigBundleRequest(
                roots=[identity("dependent")], revision_id=2, include_dependencies=True
            )
        )
        assert len(bundle.files) == 2
        assert sum(len(item.yaml_bytes) for item in bundle.files) == exact_bytes

        restart_with_limits(configurable_live, max_dependencies=2, max_bundle_bytes=exact_bytes - 1)
        with pytest.raises(grpc.aio.AioRpcError) as rejected:
            await repository.GetConfigBundle(
                pb.GetConfigBundleRequest(
                    roots=[identity("dependent")], revision_id=2, include_dependencies=True
                )
            )
        assert rejected.value.code() == grpc.StatusCode.RESOURCE_EXHAUSTED
        assert rejected.value.details() == "configuration bundle exceeds response limit"
    finally:
        await channel.close()


@pytest.mark.asyncio
async def test_grpc_transport_allows_bundle_at_exact_yaml_quota(
    configurable_live: LiveConfiguration,
) -> None:
    start_with_limits(configurable_live, max_dependencies=100, max_bundle_bytes=8 * 1024 * 1024)
    repository, _common, channel = configurable_live.stubs("deployer")
    try:
        await publish_dependency_pair(repository)
        root = await repository.GetConfig(
            pb.GetConfigRequest(config_id=identity("root"), revision_id=2)
        )
        dependent = await repository.GetConfig(
            pb.GetConfigRequest(config_id=identity("dependent"), revision_id=2)
        )
        exact_bytes = len(root.document.yaml_bytes) + len(dependent.document.yaml_bytes)
        restart_with_limits(configurable_live, max_dependencies=2, max_bundle_bytes=exact_bytes)
        bundle = await repository.GetConfigBundle(
            pb.GetConfigBundleRequest(
                roots=[identity("dependent")], revision_id=2, include_dependencies=True
            )
        )
        assert bundle.total_bytes == exact_bytes
    finally:
        await channel.close()
