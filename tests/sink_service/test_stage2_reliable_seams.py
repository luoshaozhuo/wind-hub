"""Pure, broker-free seams of ReliableDeliveryPort.

The durable ACK / NAK / InProgress / DLQ / restart state machine is only
exercised against a real JetStream broker in the Gate-4 system suite (mocks are
not accepted as evidence there). These tests pin the deterministic logic that
needs no broker: target capability validation, the durable identity, source
filtering and the cold observability surface.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from contracts.sample_message import SampleMessage
from sink_service.application.config import validate_snapshot
from sink_service.application.delivery_definition import DeliveryDefinition
from sink_service.domain.config import ConfigurationError, ValidatedSnapshot
from sink_service.infrastructure.reliable_runtime import ReliableDeliveryPort
from tests.sink_service._stage1_fixtures import snapshot_docs, snapshot_files

pytestmark = [pytest.mark.unit, pytest.mark.contract]

KAFKA_TEMPLATE = "configs/templates/sinks/kafka_history_1.0.0.yaml"
_DEFINITION = DeliveryDefinition("power_history", "history_kafka", "reliable", "START")


def _port() -> ReliableDeliveryPort:
    snapshot = validate_snapshot(snapshot_files(snapshot_docs()))
    return ReliableDeliveryPort(_DEFINITION, snapshot, pool=object())


def _unchecked_port(mutate) -> ReliableDeliveryPort:
    """Build a port whose template bytes were altered after config validation.

    The negative cases are deliberately rejected earlier by ``validate_snapshot``;
    to reach the adapter-level ``_validate_target`` guard we reuse a valid
    snapshot's parsed maps and only swap in the mutated template file.
    """
    valid = validate_snapshot(snapshot_files(snapshot_docs()))
    docs = snapshot_docs()
    mutate(docs)
    snapshot = ValidatedSnapshot(
        revision=valid.revision,
        files=snapshot_files(docs),
        service=valid.service,
        sinks=valid.sinks,
        deliveries=valid.deliveries,
    )
    return ReliableDeliveryPort(_DEFINITION, snapshot, pool=object())


def _kafka_doc(docs: dict) -> dict:
    return docs[KAFKA_TEMPLATE]


def test_validate_target_accepts_declared_at_least_once_kafka() -> None:
    _port()._validate_target()  # fixture default: kafka + allow_at_least_once


def test_require_dedup_needs_exact_two_field_identity() -> None:
    def valid(docs: dict) -> None:
        output = _kafka_doc(docs)["output"]
        output["business_duplicate_policy"] = "require_dedup"
        output["dedup_identity_fields"] = ["delivery_id", "sample_id"]

    _unchecked_port(valid)._validate_target()

    def wrong(docs: dict) -> None:
        output = _kafka_doc(docs)["output"]
        output["business_duplicate_policy"] = "require_dedup"
        output["dedup_identity_fields"] = ["sample_id"]

    with pytest.raises(ConfigurationError, match="require_dedup needs"):
        _unchecked_port(wrong)._validate_target()


def test_unknown_duplicate_policy_is_rejected() -> None:
    def mutate(docs: dict) -> None:
        _kafka_doc(docs)["output"]["business_duplicate_policy"] = "best_effort"

    with pytest.raises(ConfigurationError, match="duplicate policy missing"):
        _unchecked_port(mutate)._validate_target()


def test_reliable_redis_requires_idempotent_capability() -> None:
    def non_idempotent(docs: dict) -> None:
        template = _kafka_doc(docs)
        template["kind"] = "redis"
        template["capabilities"]["reliable"] = True
        template["capabilities"]["idempotent"] = False

    with pytest.raises(ConfigurationError, match="atomic idempotent"):
        _unchecked_port(non_idempotent)._validate_target()

    def idempotent(docs: dict) -> None:
        template = _kafka_doc(docs)
        template["kind"] = "redis"
        template["capabilities"]["reliable"] = True
        template["capabilities"]["idempotent"] = True

    _unchecked_port(idempotent)._validate_target()


def test_non_reliable_sink_and_unknown_kind_are_rejected() -> None:
    def not_reliable(docs: dict) -> None:
        _kafka_doc(docs)["capabilities"]["reliable"] = False

    with pytest.raises(ConfigurationError, match="Reliable capability"):
        _unchecked_port(not_reliable)._validate_target()

    def unknown_kind(docs: dict) -> None:
        template = _kafka_doc(docs)
        template["kind"] = "modbus_tcp"
        template["capabilities"]["reliable"] = True

    with pytest.raises(ConfigurationError, match="unsupported Reliable Sink Adapter"):
        _unchecked_port(unknown_kind)._validate_target()


def test_durable_identity_is_deterministic_and_deployment_scoped() -> None:
    port = _port()
    snapshot = port._snapshot
    first = ReliableDeliveryPort._durable_id(snapshot, "power_history")
    second = ReliableDeliveryPort._durable_id(snapshot, "power_history")
    assert first == second and first.startswith("WH_SINK_")
    suffix = first.removeprefix("WH_SINK_")
    assert len(suffix) == 24 and all(char in "0123456789abcdef" for char in suffix)
    # A different delivery or deployment yields an isolated durable consumer.
    assert ReliableDeliveryPort._durable_id(snapshot, "other_delivery") != first
    renamed = snapshot_docs()
    renamed["configs/services/sink-realtime.yaml"]["deployment_id"] = "prod-b"
    renamed["configs/services/sink-reliable.yaml"]["deployment_id"] = "prod-b"
    other_snapshot = validate_snapshot(snapshot_files(renamed))
    assert ReliableDeliveryPort._durable_id(other_snapshot, "power_history") != first


def _sample(**overrides: object) -> SampleMessage:
    now = datetime.now(UTC)
    params: dict[str, object] = {
        "wind_farm_id": "farm-a",
        "collector_id": "collector-1",
        "task_id": "task-1",
        "device_id": "WT001",
        "point_id": "active_power",
        "sample_id": "s1",
        "value": 1.0,
        "quality": "good",
        "sample_time": now,
        "publish_time": now,
    }
    params.update(overrides)
    return SampleMessage(**params)  # type: ignore[arg-type]


def test_matches_honours_declared_source_filter() -> None:
    port = _port()
    assert port._matches(_sample()) is True
    assert port._matches(_sample(device_id="WT999")) is False
    assert port._matches(_sample(point_id="other_point")) is False
    assert port._matches(_sample(wind_farm_id="farm-b")) is False
    assert port._matches(_sample(collector_id="collector-2")) is False


def test_cold_observability_surface_is_honest() -> None:
    port = _port()
    inspection = port.inspect()
    assert inspection["source"] == "DISCONNECTED"
    assert inspection["consumer"] == "NOT_BOUND"
    assert inspection["delivery"] == "STOPPED"
    assert inspection["sink"] == "NOT_ATTACHED"
    assert inspection["io_inflight"] == "False"
    # Before any broker connection there is no backlog figure to fabricate.
    metrics = port.metrics
    assert "backlog_messages" not in metrics
    assert metrics["received_total"] == 0.0
    assert metrics["dead_letter_total"] == 0.0
    assert port.last_error == ""
