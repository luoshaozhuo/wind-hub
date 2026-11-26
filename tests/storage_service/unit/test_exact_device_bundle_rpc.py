"""Historical device lookup uses one exact, immutable Configuration selector."""

from unittest.mock import AsyncMock, Mock

import pytest

from contracts import configuration_repository_pb2 as pb
from storage_service.application.ingestion import ConfigHistoryUnavailableError
from storage_service.infrastructure.adapters.outbound.configuration_grpc import (
    ConfigurationGrpcClient,
)


@pytest.mark.asyncio
async def test_historical_device_closure_uses_direct_file_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONFIG_TOKEN", "a" * 40)
    archive = Mock()
    client = ConfigurationGrpcClient(
        endpoint="localhost:12345",
        token_env="TEST_CONFIG_TOKEN",
        archive=archive,
    )
    stub = Mock()
    stub.GetConfigBundle = AsyncMock(
        return_value=pb.GetConfigBundleResponse(revision_id=0)
    )
    client._stub = stub
    with pytest.raises(ConfigHistoryUnavailableError, match="malformed"):
        await client.fetch_exact_device_closure(
            device_config_id="device_wtg001_v1.2.3"
        )
    stub.GetConfigBundle.assert_awaited_once()
    request = stub.GetConfigBundle.await_args.args[0]
    assert request.exact_roots[0].config_id.config_type == "device"
    assert request.exact_roots[0].config_id.config_id == "wtg001"
    assert request.exact_roots[0].file_version == "1.2.3"
    assert not request.exact_roots[0].version_id
    assert request.include_dependencies
    stub.ListConfigVersions.assert_not_called()
    archive.persist.assert_not_called()


@pytest.mark.asyncio
async def test_historical_device_closure_rejects_invalid_file_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_CONFIG_TOKEN", "a" * 40)
    client = ConfigurationGrpcClient(
        endpoint="localhost:12345",
        token_env="TEST_CONFIG_TOKEN",
        archive=Mock(),
    )
    with pytest.raises(ConfigHistoryUnavailableError, match="invalid exact"):
        await client.fetch_exact_device_closure(
            device_config_id="device_wtg001_vlatest"
        )
