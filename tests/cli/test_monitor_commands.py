"""Five-service monitor CLI requests against real local gRPC transports."""

from typing import Any

import pytest

from contracts import (
    collector_pb2,
    commander_pb2,
    common_pb2,
    data_generator_pb2,
    industrial_gateway_pb2,
    sink_service_pb2,
)

pytestmark = pytest.mark.contract


@pytest.mark.parametrize("service", ("collector", "gateway", "commander", "sink", "data-generator"))
def test_monitor_reads_service_status(cli_env: Any, service: str) -> None:
    cli_env.server.respond_value(
        cli_env.server.method_path("CommonService", "GetServiceStatus"),
        common_pb2.GetServiceStatusResponse(health="HEALTHY"),
    )

    result = cli_env.invoke("monitor", service)

    assert result.exit_code == 0, result.stdout
    assert result.json()["data"]["health"] == "HEALTHY"
    call = cli_env.server.calls[-1]
    assert call.path == cli_env.server.method_path("CommonService", "GetServiceStatus")


@pytest.mark.parametrize(
    ("service", "service_name", "method", "response"),
    (
        (
            "collector",
            "CollectorService",
            "GetDevice",
            collector_pb2.GetDeviceResponse(),
        ),
        (
            "gateway",
            "IndustrialGatewayService",
            "GetGateway",
            industrial_gateway_pb2.GetGatewayResponse(),
        ),
        (
            "commander",
            "CommanderService",
            "GetDevice",
            commander_pb2.GetDeviceResponse(),
        ),
        ("sink", "SinkService", "GetDelivery", sink_service_pb2.GetDeliveryResponse()),
        (
            "data-generator",
            "DataGeneratorService",
            "GetGeneration",
            data_generator_pb2.GetGenerationResponse(),
        ),
    ),
)
def test_monitor_object_uses_formal_request_identity(
    cli_env: Any,
    service: str,
    service_name: str,
    method: str,
    response: Any,
) -> None:
    cli_env.server.respond_value(cli_env.server.method_path(service_name, method), response)

    result = cli_env.invoke("monitor", service, "--object-id", "obj-1")

    assert result.exit_code == 0, result.stdout
    call = cli_env.server.calls[-1]
    assert call.path == cli_env.server.method_path(service_name, method)
    if service == "collector":
        assert call.request.device_id == "obj-1"
    else:
        assert call.request.id == "obj-1"
