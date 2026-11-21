"""Admission rules coupling Sink kind to delivery mode (P1-PROD-01 regression).

The Kafka adapter only accepts durable Reliable records, while the other Sink
kinds serve Realtime DeliveryPoint writes. A mismatched combination must be
rejected at configuration admission so no startable-but-silent Delivery exists.
"""

from __future__ import annotations

import pytest

from sink_service.domain.config import ConfigurationError
from sink_service.infrastructure.config_io import validate_snapshot
from tests.sink_service._stage1_fixtures import (
    _delivery_instance,
    snapshot_docs,
    snapshot_files,
)

pytestmark = [pytest.mark.unit, pytest.mark.contract]

KAFKA_SINK = {
    "history_kafka": {
        "template_id": "kafka_history",
        "template_version": "1.0.0",
        "connection": {"bootstrap_servers": ["192.168.1.20:9092"]},
        "overrides": {},
    }
}
REDIS_SINK = {
    "ems_redis": {
        "template_id": "redis_realtime",
        "template_version": "1.0.0",
        "connection": {"host": "192.168.1.10", "port": 6379},
        "overrides": {},
    }
}


def _validate(
    deliveries: dict,
    sinks: dict,
    include_templates: tuple[str, ...],
) -> None:
    docs = snapshot_docs(deliveries=deliveries, sinks=sinks, include_templates=include_templates)
    validate_snapshot(snapshot_files(docs))


def test_kafka_reliable_delivery_is_admitted() -> None:
    deliveries = {
        "power_history": _delivery_instance(
            "power_history", template="reliable_history", sink="history_kafka"
        )
    }
    _validate(deliveries, KAFKA_SINK, ("kafka_history", "reliable_history"))


def test_non_kafka_realtime_delivery_is_admitted() -> None:
    deliveries = {
        "ems_power": _delivery_instance("ems_power", template="realtime_power", sink="ems_redis")
    }
    _validate(deliveries, REDIS_SINK, ("redis_realtime", "realtime_power"))


def test_kafka_realtime_delivery_is_rejected() -> None:
    # A realtime template routed to the Kafka Sink has no valid wire path.
    deliveries = {"bad": _delivery_instance("bad", template="realtime_power", sink="history_kafka")}
    with pytest.raises(ConfigurationError, match="Kafka requires Reliable"):
        _validate(deliveries, KAFKA_SINK, ("kafka_history", "realtime_power"))


def test_non_kafka_reliable_delivery_is_rejected() -> None:
    # The Redis Sink only serves realtime; a reliable template must not bind to it.
    deliveries = {"bad": _delivery_instance("bad", template="reliable_history", sink="ems_redis")}
    with pytest.raises(ConfigurationError, match="requires Realtime"):
        _validate(deliveries, REDIS_SINK, ("redis_realtime", "reliable_history"))


def test_default_full_snapshot_remains_valid() -> None:
    # Both default deliveries together satisfy the coupling rules.
    docs = snapshot_docs()
    validate_snapshot(snapshot_files(docs))
