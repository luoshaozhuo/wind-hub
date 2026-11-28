"""E2E-10: full-chain Redis interruption and realtime recovery."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import nats
import pytest

from .conftest import (
    FARM,
    SINK_TARGET_POINT,
    decode_kafka_wide_point,
    wait_sink_modbus_float,
)

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


def _now() -> datetime:
    return datetime.now(UTC)


def _seconds(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds()


async def _accepted_sample_times(harness: Any, since: datetime) -> set[str]:
    """直接读取 JetStream 已受理源批，以逻辑样本时间与 Kafka v3 对账。"""
    from contracts.sample_batch import SampleBatch

    client = await nats.connect(servers=harness.stack.nats, connect_timeout=10)
    try:
        js = client.jetstream()
        info = await js.stream_info(harness.sink.history_stream)
        result: set[str] = set()
        for sequence in range(1, info.state.last_seq + 1):
            raw = await js.get_msg(harness.sink.history_stream, seq=sequence)
            batch = SampleBatch.decode(raw.data, "reliable", subject=raw.subject)
            if batch.collector_id != harness.reliable_collector or batch.device_id != "WTG_SIM01":
                continue
            result.update(
                sample.sample_time.isoformat()
                for sample in batch.samples
                if sample.point_id == "wind_speed" and sample.sample_time >= since
            )
        return result
    finally:
        await client.drain()


async def _kafka_sample_times(harness: Any, expected: set[str], since: datetime) -> list[str]:
    from aiokafka import AIOKafkaConsumer

    consumer = AIOKafkaConsumer(
        harness.sink.kafka_topic,
        bootstrap_servers=harness.stack.kafka,
        auto_offset_reset="earliest",
        group_id=f"r4e-recovery-audit-{uuid4()}",
        enable_auto_commit=False,
    )
    await consumer.start()
    observed: list[str] = []
    deadline = asyncio.get_running_loop().time() + 30
    try:
        while asyncio.get_running_loop().time() < deadline:
            batches = await consumer.getmany(timeout_ms=1000, max_records=1000)
            for records in batches.values():
                for record in records:
                    item = decode_kafka_wide_point(
                        record, device_id="WTG_SIM01", point_id="wind_speed"
                    )
                    if item is not None:
                        sample_time = datetime.fromisoformat(
                            item["sample_time"].replace("Z", "+00:00")
                        )
                        if sample_time >= since:
                            observed.append(sample_time.isoformat())
            if expected and expected.issubset(observed):
                return observed
        return observed
    finally:
        await consumer.stop()


async def test_e2e_10_redis_recovers_latest_value(full_chain: Any) -> None:
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    paused = False
    try:
        await harness.cli_call(harness.cli_rt, "collector", "start", "WTG_SIM01", "--yes")
        started = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(started["data"]["generation_id"])
        harness.generation_id = generation_id
        key = harness.redis_key("WTG_SIM01", "wind_speed")
        before = await harness.sink.wait_redis_value(
            key, timeout=90, match=lambda item: abs(float(item["value"]) - 10.0) < 1e-3
        )
        fault_at = _now()
        await asyncio.to_thread(harness.stack.pause, "redis")
        paused = True
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(harness.sink.wait_redis_value(key, timeout=10), timeout=3)
        detected_at = _now()
        await asyncio.to_thread(harness.stack.unpause, "redis")
        paused = False
        restored_at = _now()
        after = await harness.sink.wait_redis_value(
            key,
            timeout=90,
            match=lambda item: item["sample_id"] != before["sample_id"]
            and item["sample_time"] > before["sample_time"]
            and abs(float(item["value"]) - 10.0) < 1e-3,
        )
        first_good_at = _now()
        consecutive = await harness.sink.wait_redis_value(
            key,
            timeout=30,
            match=lambda item: item["sample_id"] != after["sample_id"]
            and item["sample_time"] > after["sample_time"],
        )
        assert after["collector_id"] == harness.rt_collector
        assert after["quality"] == consecutive["quality"] == "good"
        harness.record(
            "E2E-10-REDIS",
            status="PASS",
            generation_id=generation_id,
            before=before["sample_id"],
            after=after["sample_id"],
            consecutive_after=consecutive["sample_id"],
            fault_at=fault_at.isoformat(),
            detected_at=detected_at.isoformat(),
            restored_at=restored_at.isoformat(),
            first_good_at=first_good_at.isoformat(),
            detection_seconds=_seconds(fault_at, detected_at),
            recovery_seconds=_seconds(restored_at, first_good_at),
            recovery_sla="N/A: v0_27 has no numeric cross-module recovery threshold",
        )
    finally:
        if paused:
            await asyncio.to_thread(harness.stack.unpause, "redis")
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "WTG_SIM01", "--yes", check=False
        )


async def test_e2e_10_kafka_outage_holds_and_recovers(full_chain: Any) -> None:
    """Actual Collector traffic remains pending while the Kafka broker is paused."""
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    paused = False
    audit_start = _now()
    try:
        await harness.cli_call(harness.cli_rel, "collector", "start", "WTG_SIM01", "--yes")
        started = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(started["data"]["generation_id"])
        harness.generation_id = generation_id
        baseline = await harness.sink.wait_kafka_messages(min_messages=10, timeout=90)
        assert baseline
        outage_at = datetime.now(UTC)
        await asyncio.to_thread(harness.stack.pause, "kafka")
        paused = True
        await asyncio.sleep(10)
        held = await harness.sink.stream_pending()
        assert held["consumer_pending"] is not None and held["consumer_pending"] > 0, held
        detected_at = _now()
        await harness.cli_call(harness.cli_rel, "collector", "stop", "WTG_SIM01", "--yes")
        await asyncio.to_thread(harness.stack.unpause, "kafka")
        paused = False
        restored_at = _now()
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            state = await harness.sink.stream_pending()
            if state["consumer_pending"] == 0:
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError(f"Reliable backlog did not drain: {state}")

        def is_recovered(record: Any) -> bool:
            item = decode_kafka_wide_point(record, device_id="WTG_SIM01", point_id="wind_speed")
            return item is not None and (
                datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= outage_at
            )

        records = await harness.sink.wait_kafka_messages(
            min_messages=len(baseline) + 1,
            timeout=90,
            match=is_recovered,
        )
        payloads = [
            item
            for record in records
            if (
                item := decode_kafka_wide_point(
                    record, device_id="WTG_SIM01", point_id="wind_speed"
                )
            )
            is not None
        ]
        assert any(
            datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= outage_at
            for item in payloads
        )
        first_good_at = _now()
        expected_times = await _accepted_sample_times(harness, audit_start)
        observed_times = await _kafka_sample_times(harness, expected_times, audit_start)
        missing = sorted(expected_times - set(observed_times))
        duplicate_excess = len(observed_times) - len(set(observed_times))
        assert expected_times, "no JetStream-accepted source samples in audit window"
        assert not missing, f"accepted source samples missing from Kafka: {missing[:10]}"
        assert duplicate_excess == 0, f"duplicate Kafka sample identities: {duplicate_excess}"
        harness.record(
            "E2E-10-KAFKA",
            status="PASS",
            generation_id=generation_id,
            held=held,
            drained=state,
            consumer_state=await harness.sink.consumer_state(),
            accepted_count=len(expected_times),
            kafka_count=len(observed_times),
            missing=missing,
            duplicate_excess=duplicate_excess,
            fault_at=outage_at.isoformat(),
            detected_at=detected_at.isoformat(),
            restored_at=restored_at.isoformat(),
            first_good_at=first_good_at.isoformat(),
            detection_seconds=_seconds(outage_at, detected_at),
            recovery_seconds=_seconds(restored_at, first_good_at),
            recovery_sla="N/A: v0_27 has no numeric cross-module recovery threshold",
        )
    finally:
        if paused:
            await asyncio.to_thread(harness.stack.unpause, "kafka")
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        await harness.cli_call(
            harness.cli_rel, "collector", "stop", "WTG_SIM01", "--yes", check=False
        )


async def test_e2e_10_nats_recovers_both_channels(full_chain: Any) -> None:
    """Pause only this test's NATS and observe new data on both downstream channels."""
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    paused = False
    try:
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "start", "WTG_SIM01", "--yes")
        started = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(started["data"]["generation_id"])
        harness.generation_id = generation_id
        key = harness.redis_key("WTG_SIM01", "wind_speed")
        before = await harness.sink.wait_redis_value(
            key, timeout=90, match=lambda item: abs(float(item["value"]) - 10.0) < 1e-3
        )
        baseline = await harness.sink.wait_kafka_messages(min_messages=10, timeout=90)
        assert baseline
        fault_at = _now()
        await asyncio.to_thread(harness.stack.pause, "nats")
        paused = True
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(harness.sink.stream_pending(), timeout=3)
        detected_at = _now()
        await asyncio.to_thread(harness.stack.unpause, "nats")
        paused = False
        restored_at = _now()
        after = await harness.sink.wait_redis_value(
            key,
            timeout=90,
            match=lambda item: item["sample_id"] != before["sample_id"]
            and datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= restored_at,
        )
        records = await harness.sink.wait_kafka_messages(
            min_messages=20, timeout=90, offset_reset="latest"
        )
        payloads = [
            item
            for record in records
            if (
                item := decode_kafka_wide_point(
                    record, device_id="WTG_SIM01", point_id="wind_speed"
                )
            )
            is not None
        ]
        recovered_kafka = [
            item
            for item in payloads
            if datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= restored_at
        ]
        assert len({item["sample_time"] for item in recovered_kafka}) >= 2, recovered_kafka
        consecutive = await harness.sink.wait_redis_value(
            key,
            timeout=30,
            match=lambda item: item["sample_id"] != after["sample_id"]
            and item["sample_time"] > after["sample_time"],
        )
        first_good_at = _now()
        harness.record(
            "E2E-10-NATS",
            status="PASS",
            generation_id=generation_id,
            before=before["sample_id"],
            after=after["sample_id"],
            consecutive_after=consecutive["sample_id"],
            kafka_after=[item["sample_time"] for item in recovered_kafka],
            fault_at=fault_at.isoformat(),
            detected_at=detected_at.isoformat(),
            restored_at=restored_at.isoformat(),
            first_good_at=first_good_at.isoformat(),
            detection_seconds=_seconds(fault_at, detected_at),
            recovery_seconds=_seconds(restored_at, first_good_at),
            recovery_sla="N/A: v0_27 has no numeric cross-module recovery threshold",
        )
    finally:
        if paused:
            await asyncio.to_thread(harness.stack.unpause, "nats")
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "stop", "WTG_SIM01", "--yes", check=False)


async def test_e2e_10_invalid_reliable_batch_is_dlq_acked(full_chain: Any) -> None:
    """A permanent decode failure is durably dead-lettered before source ACK."""
    harness = full_chain
    assert harness.sink is not None
    before = await harness.sink.dlq_count()
    fault_at = _now()
    subject = f"wh.hist.batch.{FARM}.{harness.reliable_collector}.WTG_SIM01"
    client = await nats.connect(servers=harness.stack.nats, connect_timeout=10)
    try:
        ack = await client.jetstream().publish(subject, b"{not-a-valid-sample-batch", timeout=10)
        assert ack.stream == harness.sink.history_stream
    finally:
        await client.drain()

    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        if await harness.sink.dlq_count() > before:
            break
        await asyncio.sleep(0.5)
    else:
        raise AssertionError("invalid reliable batch never reached the full-chain DLQ")

    detected_at = _now()
    record = await harness.sink.dlq_latest()
    assert record is not None
    envelope = json.loads(record.data.decode("utf-8"))
    assert envelope["classification"] == "PERMANENT"
    assert envelope["delivery_id"] == "power_history"
    assert envelope["subject"] == subject

    ack_deadline = asyncio.get_running_loop().time() + 20
    while asyncio.get_running_loop().time() < ack_deadline:
        state = await harness.sink.consumer_state()
        if state["unacked"] == 0:
            break
        await asyncio.sleep(0.5)
    else:
        raise AssertionError(f"source remained unacked after DLQ PubAck: {state}")
    restored_at = _now()
    harness.record(
        "E2E-10-DLQ",
        status="PASS",
        source_stream=ack.stream,
        source_sequence=ack.seq,
        dlq_count=await harness.sink.dlq_count(),
        classification=envelope["classification"],
        delivery_id=envelope["delivery_id"],
        fault_at=fault_at.isoformat(),
        detected_at=detected_at.isoformat(),
        restored_at=restored_at.isoformat(),
        detection_seconds=_seconds(fault_at, detected_at),
        recovery_seconds=_seconds(fault_at, restored_at),
        recovery_sla="N/A: v0_27 has no numeric cross-module recovery threshold",
    )


async def test_e2e_10_gateway_restart_recovers_data(full_chain: Any) -> None:
    """Restart Gateway and prove a new post-fault value reaches all four outputs."""
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    gateway_stopped = False
    try:
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "start", "WTG_SIM01", "--yes")
        first = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(first["data"]["generation_id"])
        harness.generation_id = generation_id
        key = harness.redis_key("WTG_SIM01", "wind_speed")
        before = await harness.sink.wait_redis_value(
            key, timeout=90, match=lambda item: abs(float(item["value"]) - 10.0) < 1e-3
        )
        fault_at = _now()
        stopped = await harness.cli_call(harness.cli_rt, "service", "stop", "gateway", "--yes")
        gateway_stopped = True
        assert stopped["data"]["process"]["state"] == "STOPPED"
        detected_at = _now()
        await harness.cli_call(
            harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
        )
        restarted = await harness.cli_call(harness.cli_rt, "service", "start", "gateway", "--yes")
        gateway_stopped = False
        assert restarted["data"]["business"]["health"] == "HEALTHY"
        gateway_ready_at = _now()
        await harness.poll(
            harness.cli_rt,
            "data-generator",
            "status",
            generation_id,
            predicate=lambda data: data.get("generation", {}).get("actual_state") == "STOPPED",
            timeout=90,
        )
        harness.generation_id = None
        generation_id = None
        second = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(second["data"]["generation_id"])
        assert generation_id != first["data"]["generation_id"]
        harness.generation_id = generation_id
        restored_at = datetime.now(UTC)
        updated = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "variables",
            "update",
            "--generation-id",
            generation_id,
            "--device-id",
            "WTG_SIM01",
            "--request-id",
            str(uuid4()),
            "--point-id",
            "wind_speed_mean",
            "--value",
            "12",
            "--yes",
        )
        assert updated["data"]["accepted"] is True
        after = await harness.sink.wait_redis_value(
            key,
            timeout=90,
            match=lambda item: item["sample_id"] != before["sample_id"]
            and datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= restored_at
            and abs(float(item["value"]) - 12.0) < 1e-3,
        )
        gateway = await harness.poll(
            harness.cli_rt,
            "gateway",
            "read",
            "gw-wtg_sim01",
            SINK_TARGET_POINT,
            predicate=lambda data: bool(data.get("values"))
            and data["values"][0]["quality"] == "GOOD"
            and abs(float(data["values"][0]["value"].get("double_value", 0)) - 12.0) < 1e-3,
            timeout=90,
        )
        modbus_value, modbus_quality = await wait_sink_modbus_float(
            harness.sink_modbus_port, 12.0, timeout=90
        )
        assert modbus_quality == 0
        records = await harness.sink.wait_kafka_messages(
            min_messages=20, timeout=90, offset_reset="latest"
        )
        payloads = [
            item
            for record in records
            if (
                item := decode_kafka_wide_point(
                    record, device_id="WTG_SIM01", point_id="wind_speed"
                )
            )
            is not None
        ]
        kafka = next(
            (
                item
                for item in payloads
                if datetime.fromisoformat(item["sample_time"].replace("Z", "+00:00")) >= restored_at
                and abs(float(item["value"]) - 12.0) < 1e-3
            ),
            None,
        )
        assert kafka is not None, payloads
        first_good_at = _now()
        harness.record(
            "E2E-10-GATEWAY",
            status="PASS",
            generation_id=generation_id,
            effective_step=updated["data"]["effective_step"],
            redis_sample_id=after["sample_id"],
            gateway=gateway["values"][0],
            modbus={"value": modbus_value, "quality": modbus_quality},
            kafka_sample_time=kafka["sample_time"],
            old_generation_state="STOPPED",
            fault_at=fault_at.isoformat(),
            detected_at=detected_at.isoformat(),
            gateway_ready_at=gateway_ready_at.isoformat(),
            restored_at=restored_at.isoformat(),
            first_good_at=first_good_at.isoformat(),
            detection_seconds=_seconds(fault_at, detected_at),
            service_recovery_seconds=_seconds(fault_at, gateway_ready_at),
            recovery_seconds=_seconds(gateway_ready_at, first_good_at),
            recovery_sla="N/A: v0_27 has no numeric cross-module recovery threshold",
        )
    finally:
        if gateway_stopped:
            await harness.cli_call(
                harness.cli_rt, "service", "start", "gateway", "--yes", check=False
            )
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "stop", "WTG_SIM01", "--yes", check=False)
