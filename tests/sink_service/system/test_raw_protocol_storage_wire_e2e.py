"""Real JetStream -> Sink reliable -> Kafka RAW/PROTOCOL wire and provenance.

The process and middleware are real; no mocked delivery adapter is used.
Storage's production Kafka decoder checks the consumed record at the boundary.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest
import yaml

from collector.infrastructure.adapters.outbound.publisher import CollectorPublisher
from core.application import ProtocolSample
from sink_service.infrastructure.adapters.outbound.kafka_wire import DataLevel
from storage_service.domain.sample import DataLevel as StorageLevel
from storage_service.domain.sample import StorageSample
from tests.sink_service._stage1_fixtures import template_path
from tests.sink_service.system.conftest import COLLECTOR_ID, DEVICE_ID, WIND_FARM_ID, SinkSystemHarness

pytestmark = [
    pytest.mark.system,
    pytest.mark.reliability,
    pytest.mark.docker,
    pytest.mark.real_service,
    pytest.mark.slow,
    pytest.mark.nats,
    pytest.mark.kafka,
]


@pytest.mark.parametrize("level", ["raw", "protocol"])
async def test_reliable_sink_preserves_data_level_and_sample_identity(
    sink_harness: SinkSystemHarness, level: DataLevel
) -> None:
    sink_harness.write_dual_channel_root()
    template = sink_harness.root / template_path("reliable_history")
    document = yaml.safe_load(template.read_text(encoding="utf-8"))
    document["output"]["data_level"] = level
    template.write_text(
        yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
    )
    await sink_harness.provision_streams()
    await sink_harness.start()
    try:
        # Use the real Collector batch builder and JetStream publisher, not a
        # hand-built SampleBatch. This verifies source-byte capture into v3.
        source = bytes((0, 127, 128, 255))
        producer = CollectorPublisher(
            servers=[sink_harness.stack.nats],
            site_id=WIND_FARM_ID,
            collector_id=COLLECTOR_ID,
            delivery="reliable",
        )
        await producer.start()
        try:
            await producer.publish(
                DEVICE_ID,
                (
                    ProtocolSample(
                        point_id="p000",
                        value=821.0,
                        timestamp=datetime.now(UTC),
                        raw_payload=source,
                    ),
                ),
                device_type="wtg",
                point_table_id="point_table_modbus_wtg_sim_v1.0.0",
                task_id="real-collector-task",
            )
            deadline = asyncio.get_running_loop().time() + 30
            while producer.pubacked < 1 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
            assert producer.pubacked == 1, producer.last_error
        finally:
            await producer.stop()
        markers = [item["sample_id"] for item in producer.last_sample_evidence]
        assert len(markers) == 1
        marker = markers[0]
        records = await sink_harness.wait_kafka_messages(
            min_messages=1,
            timeout=60,
            match=lambda message: marker.encode() in message.value,
        )
        matching = [
            record for record in records
            if json.loads(record.value).get("sample_id") == marker
        ]
        assert len(matching) == 1, "one Collector sample must reach Kafka"
        wire = json.loads(matching[0].value)
        assert wire["data_level"] == level
        assert wire["task_id"] == "real-collector-task"
        assert wire["point_table_id"] == "point_table_modbus_wtg_sim_v1.0.0"
        assert wire["collector_id"] == COLLECTOR_ID
        assert wire["device_id"] == DEVICE_ID
        assert wire["sample_id"] == marker
        assert wire["batch_id"]
        sample = StorageSample.from_kafka_json(matching[0].value)
        assert sample.sample_id == marker
        assert sample.batch_id == wire["batch_id"]
        assert sample.task_id == "real-collector-task"
        if level == "raw":
            assert sample.data_level is StorageLevel.RAW
            assert wire["value"] == source.hex()
            assert sample.value == source
        else:
            assert sample.data_level is StorageLevel.PROTOCOL
            assert sample.value == 821.0
    finally:
        await sink_harness.stop()
