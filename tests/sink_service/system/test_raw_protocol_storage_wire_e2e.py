"""Real JetStream -> Sink reliable -> Kafka RAW/PROTOCOL wire and provenance.

The process and middleware are real; no mocked delivery adapter is used.
Storage's production Kafka decoder checks the consumed record at the boundary.
"""

from __future__ import annotations

import json

import pytest
import yaml

from sink_service.infrastructure.adapters.outbound.kafka_wire import DataLevel
from storage_service.domain.sample import DataLevel as StorageLevel
from storage_service.domain.sample import StorageSample
from tests.sink_service._stage1_fixtures import template_path
from tests.sink_service.system.conftest import SinkSystemHarness

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
        batch = sink_harness.make_batch("reliable", seq=821, points=1)
        source = bytes((0, 127, 128, 255))
        item = batch.samples[0].model_copy(update={"raw_payload_hex": source.hex()})
        batch = batch.model_copy(update={"samples": (item,)})
        ack = await sink_harness.publish_reliable(batch)
        assert ack.stream == sink_harness.history_stream

        marker = item.sample_id
        records = await sink_harness.wait_kafka_messages(
            min_messages=1,
            timeout=60,
            match=lambda message: marker.encode() in message.value,
        )
        matching = [
            record for record in records
            if json.loads(record.value).get("sample_id") == marker
        ]
        assert len(matching) == 1, "one source sample must reach the Kafka output"
        wire = json.loads(matching[0].value)
        assert wire["data_level"] == level
        assert wire["task_id"] == batch.task_id
        assert wire["batch_id"] == batch.batch_id
        assert wire["point_table_id"] == batch.point_table_id
        assert wire["collector_id"] == batch.collector_id
        assert wire["sample_id"] == marker
        sample = StorageSample.from_kafka_json(matching[0].value)
        assert sample.sample_id == marker
        assert sample.batch_id == batch.batch_id
        assert sample.task_id == batch.task_id
        if level == "raw":
            assert sample.data_level is StorageLevel.RAW
            assert wire["value"] == source.hex()
            assert sample.value == source
        else:
            assert sample.data_level is StorageLevel.PROTOCOL
            assert sample.value == item.value
    finally:
        await sink_harness.stop()
