"""CLI-launched Configuration to Storage authoritative Deployment integration."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from contracts import configuration_repository_pb2 as pb
from storage_service.infrastructure.adapters.outbound.configuration_archive import (
    ImmutableConfigArchive,
)
from storage_service.infrastructure.adapters.outbound.configuration_grpc import (
    ConfigurationGrpcClient,
)
from tests.support.configuration_live import LiveConfiguration

pytestmark = [pytest.mark.integration, pytest.mark.real_service]
ROOT = Path(__file__).resolve().parents[3]


def _input(kind: str, identity: str, version: str, document: dict[str, Any]) -> pb.ConfigDocumentInput:
    return pb.ConfigDocumentInput(
        config_id=pb.ConfigId(config_type=kind, config_id=identity),
        file_name=f"{kind}_{identity}_v{version}.yaml",
        yaml_bytes=yaml.safe_dump(document, sort_keys=False).encode("utf-8"),
    )


def _documents() -> list[pb.ConfigDocumentInput]:
    service = yaml.safe_load(
        (ROOT / "configs/services/service_storage-main_v1.1.0.yaml").read_bytes()
    )
    return [
        _input("units", "standard", "1.0.0", {
            "schema_version": 1, "file_version": "1.0.0",
            "quantities": {}, "units": {},
        }),
        _input("business_points", "standard", "1.0.0", {
            "schema_version": 1, "file_version": "1.0.0",
            "business_points": {"power": {"unit": "kW"}},
        }),
        _input("point_table", "storage-points", "1.0.0", {
            "schema_version": 1, "file_version": "1.0.0",
            "template_id": "storage-points", "protocol": "modbus",
            "device_type": "turbine", "points": {"power": {"unit": "kW"}},
        }),
        _input("device_model", "storage-model", "1.0.0", {
            "schema_version": 1, "file_version": "1.0.0",
            "template_id": "storage-model", "device_type_id": "turbine",
            "point_table": {"id": "storage-points", "version": "1.0.0"},
        }),
        _input("site", "example_site", "1.0.0", {
            "schema_version": 1, "file_version": "1.0.0",
            "site_id": "farm-a",
        }),
        _input("device", "farm-a", "1.0.0", {
            "schema_version": 1, "file_version": "1.0.0",
            "devices": {"WTG01": {"model": {"id": "storage-model", "version": "1.0.0"}}},
        }),
        _input("service", "storage-main", "1.1.0", service),
        _input("deployment", "prod-a", "1.0.0", {
            "schema_version": 1, "file_version": "1.0.0", "deployment_id": "prod-a",
            "services": {
                "storage-main": {"config_id": "storage-main", "file_version": "1.1.0"}
            },
            "instances": {
                "site": {"example_site": {"file_version": "1.0.0"}},
                "device": {"farm-a": {"file_version": "1.0.0"}},
            },
        }),
    ]


async def _publish(live: LiveConfiguration) -> int:
    repository, _, channel = live.stubs("deployer")
    try:
        current = await repository.GetCurrentRevision(
            pb.GetCurrentRevisionRequest(), timeout=10
        )
        before = current.revision_id
        proposal = await repository.CreateCandidate(
            pb.CreateCandidateRequest(
                request_id=f"storage-f3-create-{before + 1}",
                expected_revision_id=before,
                documents=_documents(),
            ),
            timeout=15,
        )
        assert proposal.valid, proposal.issues
        published = await repository.PublishCandidate(
            pb.PublishCandidateRequest(
                request_id=f"storage-f3-publish-{before + 1}",
                candidate_id=proposal.candidate_id,
                expected_revision_id=before,
            ),
            timeout=15,
        )
        return int(published.revision_id)
    finally:
        await channel.close()


async def test_real_configuration_service_selected_closure(
    live: LiveConfiguration, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    revision = await _publish(live)
    assert revision >= 1
    from tests.support.bearer import ensure_bearer

    actor_env = ensure_bearer("deployer").removeprefix("env:")
    monkeypatch.setenv("WIND_HUB_STORAGE_F3_CONFIG_TOKEN", os.environ[actor_env])
    client = ConfigurationGrpcClient(
        endpoint=f"127.0.0.1:{live.port}",
        token_env="WIND_HUB_STORAGE_F3_CONFIG_TOKEN",
        archive=ImmutableConfigArchive(tmp_path / "archive"),
    )
    await client.open()
    try:
        assert await client.get_current_revision() == revision
        bundle = await client.fetch_service_bundle(
            deployment_id="prod-a", service_id="storage-main"
        )
        assert bundle.revision_id == revision
        assert len(bundle.target_revision) == 64
        assert bundle.configuration.service_id == "storage-main"
        assert bundle.configuration.config_sources.instances.site == "example_site"
        assert tuple(item.device_id for item in bundle.device_bindings) == ("WTG01",)
        table = "point_table_storage-points_v1.0.0"
        assert table in bundle.point_table_versions
        historical = await client.fetch_point_table_bundle(point_table_id=table)
        assert {"point_table", "business_points", "units"} <= {
            item.config_type for item in historical
        }
    finally:
        await client.close()


async def test_real_nats_broadcast_after_configuration_publication(
    live: LiveConfiguration, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = os.getenv("WIND_HUB_TEST_NATS_URL")
    if not target:
        pytest.skip("real NATS endpoint not configured")
    import nats

    connection = await nats.connect(servers=[target], connect_timeout=2)
    messages: asyncio.Queue[bytes] = asyncio.Queue()

    async def on_change(message: Any) -> None:
        messages.put_nowait(bytes(message.data))

    subscription = await connection.subscribe("wh.config.changed", cb=on_change)
    await connection.flush(timeout=2)
    try:
        revision = await _publish(live)
        payload = await asyncio.wait_for(messages.get(), timeout=10)
        assert revision > 0
        assert payload
    finally:
        await subscription.unsubscribe()
        await connection.close()
