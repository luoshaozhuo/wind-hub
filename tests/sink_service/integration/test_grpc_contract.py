"""Method-level gRPC boundary tests without a network or broker.

The full 13-RPC surface over real mTLS is covered by the dual-process system
test. Here the servicer is driven directly with a fake servicer context so the
error taxonomy, argument validation and read-only diagnostic adjudication can
be asserted deterministically.
"""

from __future__ import annotations

import grpc
import pytest

from contracts import common_pb2, sink_service_pb2
from sink_service.application.config import validate_snapshot
from sink_service.application.config_manager import ConfigManager
from sink_service.application.delivery_registry import DeliveryRegistry
from sink_service.application.observability import Observability
from sink_service.domain.config import (
    CandidateNotFoundError,
    ConfigurationError,
    InvalidConfigurationError,
    RevisionConflictError,
    WriterRequiredError,
)
from sink_service.infrastructure.grpc_server import (
    CommonServiceServer,
    SinkServiceServer,
    _status_for,
)
from tests.sink_service._stage1_fixtures import snapshot_docs, snapshot_files

pytestmark = [pytest.mark.integration, pytest.mark.contract]


class _GrpcAbortError(Exception):
    def __init__(self, code: grpc.StatusCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class FakeContext:
    def abort(self, code: grpc.StatusCode, message: str) -> None:
        raise _GrpcAbortError(code, message)

    def auth_context(self) -> dict:
        return {}


@pytest.fixture
def harness() -> tuple[CommonServiceServer, SinkServiceServer, Observability]:
    snapshot = validate_snapshot(snapshot_files(snapshot_docs()))
    manager = ConfigManager(None, snapshot, writer=False)  # type: ignore[arg-type]
    registry = DeliveryRegistry("realtime", snapshot)
    observability = Observability(window_ms=60000)
    common = CommonServiceServer(
        manager,
        registry,
        service_id="sink-west",
        role="realtime",
        observability=observability,
    )
    sink = SinkServiceServer(registry, manager, observability)
    return common, sink, observability


def test_status_maps_domain_taxonomy_to_grpc_codes() -> None:
    assert _status_for(InvalidConfigurationError("x")) == grpc.StatusCode.INVALID_ARGUMENT
    assert _status_for(RevisionConflictError("x")) == grpc.StatusCode.ABORTED
    assert _status_for(WriterRequiredError("x")) == grpc.StatusCode.PERMISSION_DENIED
    assert _status_for(CandidateNotFoundError("x")) == grpc.StatusCode.NOT_FOUND
    assert _status_for(ConfigurationError("x")) == grpc.StatusCode.FAILED_PRECONDITION


async def test_service_info_and_role_specific_capabilities(
    harness: tuple[CommonServiceServer, SinkServiceServer, Observability],
) -> None:
    common, _, _ = harness
    info = await common.GetServiceInfo(common_pb2.Empty(), FakeContext())
    assert info.service_id == "sink-west"
    assert info.process_role == "realtime"
    assert info.contract_version == "v1"

    caps = await common.GetCapabilities(common_pb2.Empty(), FakeContext())
    assert len(caps.rpc_names) == 13
    assert "StopDelivery" in caps.rpc_names
    # v1_20 pins JetStream as a distinct reliable protocol alongside NATS Core.
    assert set(caps.protocols) == {
        "nats_core",
        "jetstream",
        "redis",
        "kafka",
        "modbus_tcp",
    }
    assert "realtime_latest_value" in caps.features


async def test_healthy_status_when_runtime_matches_disk(
    harness: tuple[CommonServiceServer, SinkServiceServer, Observability],
) -> None:
    common, _, _ = harness
    status = await common.GetServiceStatus(common_pb2.Empty(), FakeContext())
    assert status.health == "HEALTHY"
    configuration = next(item for item in status.dependencies if item.name == "configuration")
    assert configuration.health == "RUNNING"


async def test_metrics_window_validation_units_and_cold_readiness(
    harness: tuple[CommonServiceServer, SinkServiceServer, Observability],
) -> None:
    common, _, _ = harness
    with pytest.raises(_GrpcAbortError) as excinfo:
        await common.GetMetrics(common_pb2.GetMetricsRequest(window_ms=1), FakeContext())
    assert excinfo.value.code == grpc.StatusCode.INVALID_ARGUMENT

    response = await common.GetMetrics(common_pb2.GetMetricsRequest(window_ms=60000), FakeContext())
    by_name = {item.name: item for item in response.metrics}
    assert by_name["metrics_snapshot_ready"].value == 0.0
    assert by_name["metrics_snapshot_ready"].unit == "bool"
    # Every emitted metric carries the contract window and a unit.
    assert all(item.window_ms == 60000 and item.unit for item in response.metrics)


async def test_events_reject_oversized_page_and_return_recorded_events(
    harness: tuple[CommonServiceServer, SinkServiceServer, Observability],
) -> None:
    common, _, observability = harness
    observability.event("ERROR", "power_history", "DLQ_01", "dead letter emitted")
    with pytest.raises(_GrpcAbortError) as excinfo:
        await common.GetEvents(common_pb2.GetEventsRequest(page_size=501), FakeContext())
    assert excinfo.value.code == grpc.StatusCode.INVALID_ARGUMENT

    response = await common.GetEvents(
        common_pb2.GetEventsRequest(page_size=10, filter="DLQ"), FakeContext()
    )
    assert len(response.events) == 1
    assert response.events[0].error_code == "DLQ_01"
    assert response.next_page_token == ""


async def test_diagnose_rejects_unknown_stage_and_adjudicates_configuration(
    harness: tuple[CommonServiceServer, SinkServiceServer, Observability],
) -> None:
    common, _, _ = harness
    with pytest.raises(_GrpcAbortError) as excinfo:
        await common.Diagnose(
            common_pb2.DiagnoseRequest(stages=["bogus"], timeout_ms=1000), FakeContext()
        )
    assert excinfo.value.code == grpc.StatusCode.INVALID_ARGUMENT

    response = await common.Diagnose(
        common_pb2.DiagnoseRequest(stages=["configuration", "target"], timeout_ms=1000),
        FakeContext(),
    )
    by_stage = {stage.stage: stage for stage in response.stages}
    assert by_stage["configuration"].status == "SUCCESS"
    # No side-effect-free target probe exists; this must stay honestly limited.
    assert by_stage["target"].status == "NOT_SUPPORTED"
    assert response.overall_status == "PARTIAL"
    assert response.limitations


async def test_diagnose_unknown_target_is_not_found(
    harness: tuple[CommonServiceServer, SinkServiceServer, Observability],
) -> None:
    common, _, _ = harness
    with pytest.raises(_GrpcAbortError) as excinfo:
        await common.Diagnose(
            common_pb2.DiagnoseRequest(stages=["delivery"], target_id="ghost", timeout_ms=1000),
            FakeContext(),
        )
    assert excinfo.value.code == grpc.StatusCode.NOT_FOUND


async def test_list_validates_channel_and_get_unknown_delivery_is_not_found(
    harness: tuple[CommonServiceServer, SinkServiceServer, Observability],
) -> None:
    _, sink, _ = harness
    with pytest.raises(_GrpcAbortError) as excinfo:
        await sink.ListDeliveries(
            sink_service_pb2.ListDeliveriesRequest(channel="satellite"), FakeContext()
        )
    assert excinfo.value.code == grpc.StatusCode.INVALID_ARGUMENT

    empty = await sink.ListDeliveries(
        sink_service_pb2.ListDeliveriesRequest(channel="realtime"), FakeContext()
    )
    assert empty.deliveries == []

    with pytest.raises(_GrpcAbortError) as excinfo:
        await sink.GetDelivery(common_pb2.ObjectId(id="ghost"), FakeContext())
    assert excinfo.value.code == grpc.StatusCode.NOT_FOUND
