"""Real JetStream -> Sink reliable -> Kafka RAW/PROTOCOL wire and provenance.

The process and middleware are real; no mocked delivery adapter is used.
Storage's production Kafka JSON v3 decoder checks device-wide records.
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
from storage_service.domain.wide_record import WideDataLevel, WideRecord
from tests.sink_service._stage1_fixtures import template_path
from tests.sink_service.system.conftest import (
    COLLECTOR_ID,
    DEVICE_ID,
    WIND_FARM_ID,
    SinkSystemHarness,
)

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
    template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
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
        records = await sink_harness.wait_kafka_messages(
            min_messages=1,
            timeout=60,
            match=lambda message: (
                json.loads(message.value).get("schema_version") == 3
                and json.loads(message.value).get("device_id") == DEVICE_ID
                and json.loads(message.value).get("data_level") == level
            ),
        )
        matching = [
            record for record in records
            if json.loads(record.value).get("schema_version") == 3
            and json.loads(record.value).get("data_level") == level
            and json.loads(record.value).get("device_id") == DEVICE_ID
        ]
        assert matching, "Collector device row must reach Kafka"
        wire = json.loads(matching[-1].value)
        assert wire["points"]["p000"][1] == 1
        assert wire["device_config_id"]
        assert wire["wind_farm_id"] == WIND_FARM_ID
        row = WideRecord.from_kafka_json(matching[-1].value, physical_points=("p000",))
        assert row.data_level is WideDataLevel(level)
        assert row.device_id == DEVICE_ID
        if level == "raw":
            assert row.values == (source.hex(),)
        else:
            assert row.values == (821.0,)
    finally:
        await sink_harness.stop()


async def test_modbus_tcp_native_register_bytes_reach_kafka_and_storage(
    sink_harness: SinkSystemHarness,
) -> None:
    """Read an actual FC03 response, then verify unchanged bytes at storage input."""
    import struct

    from tests.core.protocol.conftest import modbus_driver
    from tests.fixtures.servers.modbus_server import ModbusMockServer
    from tests.sink_service.system.conftest import free_port

    sink_harness.write_dual_channel_root()
    template = sink_harness.root / template_path("reliable_history")
    document = yaml.safe_load(template.read_text(encoding="utf-8"))
    document["output"]["data_level"] = "raw"
    template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    await sink_harness.provision_streams()
    await sink_harness.start()
    server = ModbusMockServer(port=free_port())
    await server.start()
    try:
        driver = modbus_driver(server.port)
        await driver.connect()
        try:
            # The server encodes 1200.5 into two FC03 register words.
            # This is the protocol data field, not a synthesized engineering value.
            sample = await driver.read_one("speed")
        finally:
            await driver.close()
        assert sample.value == pytest.approx(1200.5)
        assert sample.raw_payload == struct.pack(">f", 1200.5)

        publisher = CollectorPublisher(
            servers=[sink_harness.stack.nats],
            site_id=WIND_FARM_ID,
            collector_id=COLLECTOR_ID,
            delivery="reliable",
        )
        await publisher.start()
        try:
            await publisher.publish(
                DEVICE_ID,
                (sample,),
                device_type="wtg",
                point_table_id="point_table_modbus_wtg_sim_v1.0.0",
                task_id="modbus-fc03-source",
            )
            deadline = asyncio.get_running_loop().time() + 30
            while publisher.pubacked < 1 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
            assert publisher.pubacked == 1, publisher.last_error
        finally:
            await publisher.stop()

        records = await sink_harness.wait_kafka_messages(
            min_messages=1,
            timeout=60,
            match=lambda message: (
                json.loads(message.value).get("schema_version") == 3
                and json.loads(message.value).get("device_id") == DEVICE_ID
                and json.loads(message.value).get("data_level") == "raw"
            ),
        )
        matching = [
            record for record in records
            if json.loads(record.value).get("schema_version") == 3
            and json.loads(record.value).get("data_level") == "raw"
            and json.loads(record.value).get("device_id") == DEVICE_ID
        ]
        assert matching
        record = json.loads(matching[-1].value)
        assert record["device_config_id"]
        assert record["points"]["speed"] == [struct.pack(">f", 1200.5).hex(), 1]
        stored = WideRecord.from_kafka_json(
            matching[-1].value, physical_points=("speed",)
        )
        assert stored.data_level is WideDataLevel.RAW
        assert stored.values == (sample.raw_payload.hex(),)
    finally:
        await server.stop()
        await sink_harness.stop()


async def test_raw_missing_source_bytes_goes_to_durable_dlq_without_partial_kafka_write(
    sink_harness: SinkSystemHarness,
) -> None:
    sink_harness.write_dual_channel_root()
    template = sink_harness.root / template_path("reliable_history")
    document = yaml.safe_load(template.read_text(encoding="utf-8"))
    document["output"]["data_level"] = "raw"
    template.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    await sink_harness.provision_streams()
    await sink_harness.start()
    try:
        # The first point has valid source bytes, the second has none.
        # The whole batch must be rejected before the first Kafka write.
        batch = sink_harness.make_batch("reliable", seq=822, points=2)
        first = batch.samples[0].model_copy(update={"raw_payload_hex": bytes((1, 2)).hex()})
        batch = batch.model_copy(update={"samples": (first, batch.samples[1])})
        await sink_harness.publish_reliable(batch)
        deadline = asyncio.get_running_loop().time() + 30
        while await sink_harness.dlq_count() == 0:
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("missing RAW bytes never reached durable DLQ")
            await asyncio.sleep(0.25)
        assert await sink_harness.dlq_count() == 1
        while True:
            pending = await sink_harness.stream_pending()
            if pending["durable"] and pending["consumer_pending"] == 0:
                break
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"source never ACKed after durable DLQ: {pending}")
            await asyncio.sleep(0.25)
        records = await sink_harness.wait_kafka_messages(min_messages=1, timeout=3)
        assert not records, "invalid RAW batch leaked a Kafka prefix"
    finally:
        await sink_harness.stop()
