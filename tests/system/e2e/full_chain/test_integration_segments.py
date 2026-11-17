"""R4-E I-01..I-05、I-07：共享 full_chain 的真实分段验收。"""

from __future__ import annotations

import asyncio
import json
import statistics
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import grpc
import nats
import pytest
from google.protobuf.timestamp_pb2 import Timestamp
from pymodbus.client import AsyncModbusTcpClient

from cli.runtime import Runtime
from contracts import collector_pb2 as collector
from contracts import collector_pb2_grpc as collector_rpc
from contracts import commander_pb2 as cmd
from contracts import commander_pb2_grpc as cmd_rpc
from contracts import common_pb2 as common
from contracts import common_pb2_grpc as common_rpc
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts import industrial_gateway_pb2 as gateway
from contracts import industrial_gateway_pb2_grpc as gateway_rpc
from contracts.scalar_codec import decode_scalar, encode_scalar
from tests.cli.test_r1f3_strict_cli_e2e import ADMIN_TOKEN

from .conftest import (
    REPO_CONFIGS,
    SINK_TARGET_POINT,
    FullChainHarness,
    wait_sink_modbus_float,
)
from .integration_segments import (
    DualBatchObserver,
    correlate_source,
    fixed_batch,
    float_words,
    generator_channel,
    kafka_consumer,
    load_yaml,
    mtls_channel,
    number,
    publish_batch,
    read_modbus_float,
    start_target_gateway,
    stop_target_gateway,
    utc_time,
    wait_gateway_value,
    wait_kafka_sample,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]


async def _start_generation(harness: FullChainHarness) -> str:
    started = await harness.cli_call(
        harness.cli_rt,
        "data-generator",
        "start",
        "wind-plant-physical",
        "--scenario-version",
        "1.0.0",
        "--yes",
    )
    assert started["data"]["accepted"] is True
    generation_id = str(started["data"]["generation_id"])
    harness.generation_id = generation_id
    return generation_id


async def _stop_generation(harness: FullChainHarness) -> None:
    if harness.generation_id is not None:
        await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "stop",
            harness.generation_id,
            "--yes",
            check=False,
        )
        harness.generation_id = None


async def _metric_values(runtime: Runtime, service: str) -> dict[str, float]:
    reply = await common_rpc.CommonServiceStub(runtime.channel(service)).GetMetrics(
        common.GetMetricsRequest(), timeout=runtime.target(service).timeout
    )
    return {metric.name: float(metric.value) for metric in reply.metrics}


async def _published_count(runtime: Runtime) -> int:
    reply = await collector_rpc.CollectorServiceStub(
        runtime.channel("collector")
    ).GetPublishingStatus(
        collector.GetPublishingStatusRequest(), timeout=runtime.target("collector").timeout
    )
    assert len(reply.channels) == 1
    return int(reply.channels[0].published)


@pytest.mark.timeout(180)
async def test_i01_recording_gateway_identity_window_and_dg_lease_release(
    full_chain: FullChainHarness,
) -> None:
    harness = full_chain
    owner = f"r4e-i01-{uuid4().hex}"
    channel = generator_channel(harness)
    acquired_token = ""
    try:
        generation_id = await _start_generation(harness)
        observed = await harness.poll(
            harness.cli_rt,
            "gateway",
            "read",
            "gw-wtg_sim01",
            "wind_speed",
            predicate=lambda data: bool(data.get("values"))
            and data["values"][0].get("sample_timestamp"),
            timeout=90,
        )
        sample = observed["values"][0]
        sample_time = utc_time(sample["sample_timestamp"])
        rows = harness.read_recording(generation_id)
        truth = correlate_source(
            rows, "WTG_SIM01", "wind_speed", sample_time, number(sample["value"])
        )
        assert truth["generation_id"] == generation_id
        assert isinstance(truth["sequence"], int) and truth["sequence"] > 0
        assert truth["source_time"]
        assert observed["gateway_id"] == "gw-wtg_sim01"
        assert sample["point_id"] == "wind_speed"
        assert sample["quality"] == "GOOD"
        generation = await harness.cli_call(
            harness.cli_rt, "data-generator", "status", generation_id
        )
        assert generation["data"]["generation"]["generation_id"] == generation_id
        assert generation["data"]["generation"]["scenario_id"] == "wind-plant-physical"
        assert generation["data"]["generation"]["scenario_version"] == "1.0.0"
        gateway_status = await harness.cli_call(harness.cli_rt, "gateway", "status", "gw-wtg_sim01")
        assert gateway_status["data"]["gateway"]["gateway_id"] == "gw-wtg_sim01"
        assert gateway_status["data"]["gateway"]["revision"]

        stub = gateway_rpc.IndustrialGatewayServiceStub(channel)
        request = gateway.ClaimTargetsRequest(
            gateway_id="gw-wtg_sim01",
            owner_id=owner,
            variable_ids=["wind_speed"],
            action="ACQUIRE",
            lease_ms=30000,
        )
        busy = await stub.ClaimTargets(request, timeout=10)
        assert busy.accepted is False
        assert busy.error_code == "TARGET_CLAIMED"

        await _stop_generation(harness)
        acquired = await stub.ClaimTargets(request, timeout=10)
        assert acquired.accepted is True, acquired
        assert acquired.lease_token
        acquired_token = acquired.lease_token
        released = await stub.ClaimTargets(
            gateway.ClaimTargetsRequest(
                gateway_id="gw-wtg_sim01",
                owner_id=owner,
                variable_ids=["wind_speed"],
                action="RELEASE",
                lease_token=acquired.lease_token,
            ),
            timeout=10,
        )
        assert released.accepted is True, released
        acquired_token = ""
        harness.record(
            "R4E-I01",
            generation_id=generation_id,
            sequence=truth["sequence"],
            source_time=truth["source_time"],
            source_value=truth["value"],
            gateway_sample_timestamp=sample["sample_timestamp"],
            gateway_value=number(sample["value"]),
            scenario_version="1.0.0",
            gateway_revision=gateway_status["data"]["gateway"]["revision"],
            busy_error=busy.error_code,
            acquire_after_stop=acquired.accepted,
            release_after_stop=released.accepted,
        )
    finally:
        if acquired_token:
            await gateway_rpc.IndustrialGatewayServiceStub(channel).ClaimTargets(
                gateway.ClaimTargetsRequest(
                    gateway_id="gw-wtg_sim01",
                    owner_id=owner,
                    variable_ids=["wind_speed"],
                    action="RELEASE",
                    lease_token=acquired_token,
                ),
                timeout=5,
            )
        await channel.close()
        await _stop_generation(harness)


@pytest.mark.timeout(180)
async def test_i02_stopped_active_read_no_publish_and_running_cadence(
    full_chain: FullChainHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = full_chain
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", ADMIN_TOKEN)
    runtime = Runtime(harness.cli_rt, "json")
    observer = DualBatchObserver(harness, "WTG_SIM01")
    try:
        generation_id = await _start_generation(harness)
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "WTG_SIM01", "--yes", check=False
        )
        await observer.start()
        before_stop = await _published_count(runtime)
        stopped_read = await collector_rpc.CollectorServiceStub(
            runtime.channel("collector")
        ).ReadPoints(
            collector.ReadPointsRequest(
                device_id="WTG_SIM01", point_ids=["wind_speed"], timeout_ms=3000
            ),
            metadata=runtime.get_rpc_metadata("collector", "ReadPoints"),
            timeout=10,
        )
        stopped = {sample.point_id: sample for sample in stopped_read.samples}
        assert stopped["wind_speed"].quality == "good"
        assert abs(decode_scalar(stopped["wind_speed"].value) - 10.0) < 0.6
        await asyncio.sleep(0.5)
        after_stop = await _published_count(runtime)
        assert after_stop == before_stop

        await harness.cli_call(harness.cli_rt, "collector", "start", "WTG_SIM01", "--yes")
        before_active = await _published_count(runtime)
        active_reply = await collector_rpc.CollectorServiceStub(
            runtime.channel("collector")
        ).ReadPoints(
            collector.ReadPointsRequest(
                device_id="WTG_SIM01", point_ids=["wind_speed"], timeout_ms=3000
            ),
            metadata=runtime.get_rpc_metadata("collector", "ReadPoints"),
            timeout=10,
        )
        active = active_reply.samples[0]
        after_active = await _published_count(runtime)
        # 运行中后台任务可并发发布；主动读不发布的严格隔离由 STOP 状态相等断言证明。
        assert after_active >= before_active
        active_value = float(decode_scalar(active.value))
        assert active.quality == "good"
        point_table = load_yaml(
            harness.plant
            / "templates"
            / "point_tables"
            / "point_table_modbus_wtg_sim_e2e_v1.0.0.yaml"
        )["points"]["wind_speed"]
        scale = float(point_table["scale"])
        offset = float(point_table["offset"])
        raw_value = await read_modbus_float(harness.modbus_ports["WTG_SIM01"], 0, holding=False)
        assert abs(active_value - (raw_value * scale + offset)) <= 0.6
        assert active.sample_timestamp.ToDatetime(tzinfo=UTC).tzinfo is not None

        observed = await observer.wait("realtime", 4, timeout=60)
        times: list[datetime] = []
        rows = harness.read_recording(generation_id)
        for item in observed:
            matches = [sample for sample in item.batch.samples if sample.point_id == "wind_speed"]
            assert len(matches) == 1
            sample = matches[0]
            assert sample.quality == "good"
            assert item.batch.device_id == "WTG_SIM01"
            assert item.batch.collector_id == harness.rt_collector
            correlate_source(
                rows, "WTG_SIM01", "wind_speed", sample.sample_time, number(sample.value)
            )
            times.append(sample.sample_time)
        assert len(set(times)) >= 4
        intervals = [
            (right - left).total_seconds() for left, right in zip(times, times[1:], strict=False)
        ]
        median = statistics.median(intervals)
        assert 0.1 <= median <= 0.36, median
        assert await _published_count(runtime) > after_active
        harness.record(
            "R4E-I02",
            generation_id=generation_id,
            stopped_published_before=before_stop,
            stopped_published_after=after_stop,
            active_published_before=before_active,
            active_published_after=after_active,
            scale=scale,
            offset=offset,
            active_value=active_value,
            raw_modbus_value=raw_value,
            background_batches=len(observed),
            median_period=median,
        )
    finally:
        await observer.close()
        await runtime.close()
        await harness.cli_call(
            harness.cli_rt, "collector", "stop", "WTG_SIM01", "--yes", check=False
        )
        await _stop_generation(harness)


@pytest.mark.timeout(240)
async def test_i03_dual_observers_metrics_sequences_and_sink_outputs(
    full_chain: FullChainHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = full_chain
    assert harness.sink is not None
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", ADMIN_TOKEN)
    rt_runtime = Runtime(harness.cli_rt, "json")
    hist_runtime = Runtime(harness.cli_rel, "json")
    observer = DualBatchObserver(harness, "WTG_SIM01")
    consumer = await kafka_consumer(harness, "i03")
    try:
        await observer.start()
        before_rt = await _metric_values(rt_runtime, "collector")
        before_hist = await _metric_values(hist_runtime, "collector")
        generation_id = await _start_generation(harness)
        await harness.cli_call(harness.cli_rt, "collector", "start", "WTG_SIM01", "--yes")
        await harness.cli_call(harness.cli_rel, "collector", "start", "WTG_SIM01", "--yes")
        rt_batches, hist_batches = await asyncio.gather(
            observer.wait("realtime", 2), observer.wait("reliable", 2)
        )
        after_rt = await _metric_values(rt_runtime, "collector")
        after_hist = await _metric_values(hist_runtime, "collector")
        assert after_rt["collector.publisher.client_submitted"] > before_rt.get(
            "collector.publisher.client_submitted", 0
        )
        assert after_hist["collector.publisher.pubacked"] > before_hist.get(
            "collector.publisher.pubacked", 0
        )
        sequences = [item.js_sequence for item in hist_batches]
        assert all(isinstance(sequence, int) and sequence > 0 for sequence in sequences)
        assert sequences == sorted(set(sequences))

        rows = harness.read_recording(generation_id)
        rt_samples = [item.batch.samples for item in rt_batches]
        hist_samples = [item.batch.samples for item in hist_batches]
        for batches in (rt_samples, hist_samples):
            for samples in batches:
                sample = next(item for item in samples if item.point_id == "wind_speed")
                correlate_source(
                    rows, "WTG_SIM01", "wind_speed", sample.sample_time, number(sample.value)
                )
        selected_rt = next(
            sample for sample in rt_batches[-1].batch.samples if sample.point_id == "wind_speed"
        )
        selected_hist = next(
            sample for sample in hist_batches[-1].batch.samples if sample.point_id == "wind_speed"
        )
        redis_sample = await harness.sink.wait_redis_value(
            harness.redis_key("WTG_SIM01", "wind_speed"),
            timeout=60,
            match=lambda item: utc_time(item["sample_time"]) >= selected_rt.sample_time,
        )
        kafka_sample = await wait_kafka_sample(consumer, selected_hist.sample_id)
        for item in (redis_sample, kafka_sample):
            assert item["device_id"] == "WTG_SIM01"
            assert item["point_id"] == "wind_speed"
            correlate_source(
                rows,
                "WTG_SIM01",
                "wind_speed",
                utc_time(item["sample_time"]),
                number(item["value"]),
            )
        assert redis_sample["collector_id"] == harness.rt_collector
        assert kafka_sample["collector_id"] == harness.reliable_collector
        await _stop_generation(harness)
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "stop", "WTG_SIM01", "--yes")
        harness.record(
            "R4E-I03",
            generation_id=generation_id,
            rt_subject=observer.rt_subject,
            hist_subject=observer.hist_subject,
            rt_submitted_delta=after_rt["collector.publisher.client_submitted"]
            - before_rt.get("collector.publisher.client_submitted", 0),
            reliable_pubacked_delta=after_hist["collector.publisher.pubacked"]
            - before_hist.get("collector.publisher.pubacked", 0),
            js_sequences=sequences,
            redis_sample_id=redis_sample["sample_id"],
            kafka_sample_id=kafka_sample["sample_id"],
            rt_batch_id=rt_batches[-1].batch.batch_id,
            hist_batch_id=hist_batches[-1].batch.batch_id,
        )
    finally:
        await consumer.stop()
        await observer.close()
        await rt_runtime.close()
        await hist_runtime.close()
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "stop", "WTG_SIM01", "--yes", check=False)
        await _stop_generation(harness)


@pytest.mark.timeout(180)
async def test_i04_fixed_v2_batches_reach_four_real_outputs(
    full_chain: FullChainHarness,
) -> None:
    harness = full_chain
    assert harness.sink is not None
    consumer = await kafka_consumer(harness, "i04-earliest")
    rt_id = f"r4e-i04-rt-{uuid4().hex}"
    hist_id = f"r4e-i04-hist-{uuid4().hex}"
    source_time = datetime.now(UTC) - timedelta(seconds=1)
    try:
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "stop", "WTG_SIM01", "--yes", check=False)
        rt = fixed_batch(
            harness, "realtime", sample_id=rt_id, value=314.25, source_time=source_time
        )
        hist = fixed_batch(
            harness, "reliable", sample_id=hist_id, value=271.5, source_time=source_time
        )
        published_at = datetime.now(UTC)
        await publish_batch(harness, rt)
        ack = await publish_batch(harness, hist)
        assert ack.stream == harness.sink.history_stream
        assert ack.seq > 0

        redis_sample = await harness.sink.wait_redis_value(
            harness.redis_key("WTG_SIM01", "wind_speed"),
            timeout=60,
            match=lambda item: item.get("sample_id") == rt_id,
        )
        kafka_sample = await wait_kafka_sample(consumer, hist_id)
        gateway_sample = await wait_gateway_value(
            harness, harness.cli_rt, "gw-wtg_sim01", SINK_TARGET_POINT, 314.25
        )
        modbus_value, quality = await wait_sink_modbus_float(
            harness.sink_modbus_port, 314.25, timeout=60
        )
        assert redis_sample["sample_id"] == rt_id
        assert kafka_sample["sample_id"] == hist_id
        assert redis_sample["value"] == 314.25
        assert kafka_sample["value"] == 271.5
        assert utc_time(redis_sample["sample_time"]) == source_time
        assert utc_time(kafka_sample["sample_time"]) == source_time
        gateway_time = utc_time(gateway_sample["values"][0]["sample_timestamp"])
        assert published_at <= gateway_time <= datetime.now(UTC)
        assert gateway_sample["values"][0]["point_id"] == SINK_TARGET_POINT
        assert abs(modbus_value - 314.25) <= 1e-3
        assert quality == 0
        harness.record(
            "R4E-I04",
            rt_sample_id=rt_id,
            hist_sample_id=hist_id,
            rt_value=redis_sample["value"],
            hist_value=kafka_sample["value"],
            source_time=source_time.isoformat(),
            reliable_puback_stream=ack.stream,
            reliable_puback_sequence=ack.seq,
            kafka_offset_reset="earliest",
            gateway_point=SINK_TARGET_POINT,
            gateway_update_timestamp=gateway_time.isoformat(),
            modbus_quality=quality,
            generator_started=False,
            collectors_started=False,
        )
    finally:
        await consumer.stop()


async def _audit_events(
    queue: asyncio.Queue[dict[str, Any]], point_ids: set[str]
) -> list[dict[str, Any]]:
    accepted: dict[str, dict[str, Any]] = {}
    while {str(event.get("point_id")) for event in accepted.values()} != point_ids:
        event = await asyncio.wait_for(queue.get(), timeout=60)
        if event.get("event_type") == "ACCEPTED" and event.get("point_id") in point_ids:
            accepted[str(event["command_id"])] = event
    return list(accepted.values())


@pytest.mark.timeout(300)
async def test_i05_real_modbus_control_audit_replay_auth_and_unknown(
    full_chain: FullChainHarness,
) -> None:
    harness = full_chain
    table = load_yaml(
        REPO_CONFIGS
        / "templates"
        / "point_tables"
        / "point_table_modbus_plant_controller_sim_v1.0.0.yaml"
    )["points"]
    enable_address = int(table["agc_enable"]["address"]["address"])
    power_address = int(table["agc_power_setpoint"]["address"]["address"])
    audit_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    client = await nats.connect(harness.stack.nats, connect_timeout=10)

    async def on_audit(message: Any) -> None:
        event = json.loads(message.data)
        await message.ack()
        await audit_queue.put(event)

    subscription = await client.jetstream().subscribe(
        f"commander.fullchain.{harness.token}", cb=on_audit, manual_ack=True
    )
    commander_stopped = False
    try:
        generation_id = await _start_generation(harness)
        modbus = AsyncModbusTcpClient("127.0.0.1", port=harness.modbus_ports["PPC_SIM01"])
        await modbus.connect()
        try:
            enable = await modbus.write_register(enable_address, 1, device_id=1)
            assert not enable.isError(), enable
            power = await modbus.write_registers(power_address, float_words(1500.0), device_id=1)
            assert not power.isError(), power
        finally:
            modbus.close()
        accepted = await _audit_events(audit_queue, {"agc_enable", "agc_power_setpoint"})
        command_ids = {str(event["command_id"]) for event in accepted}
        assert len(command_ids) == 2
        gateway_cert, gateway_key = harness.pki["clients"]["gateway"]
        async with mtls_channel(
            harness.endpoints["data-generator"], harness.pki["ca"], gateway_cert, gateway_key
        ) as channel:
            for event in accepted:
                command_id = str(event["command_id"])
                completed = await harness.poll(
                    harness.cli_rt,
                    "commander",
                    "result",
                    command_id,
                    predicate=lambda item: item.get("state")
                    in {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED"},
                    timeout=60,
                )
                assert completed["state"] == "SUCCEEDED"
                assert completed["outcome"] == "CONFIRMED_SUCCESS"
                assert completed["io_settled"] is True
                execution = await dg_rpc.DataGeneratorServiceStub(channel).GetInputExecution(
                    dg.GetInputExecutionRequest(generation_id=generation_id, request_id=command_id),
                    timeout=10,
                )
                assert execution.request_id == command_id
                assert execution.state == "APPLIED"
                assert execution.effective_step > 0

        replay_device = await harness.cli_call(harness.cli_rt, "commander", "device", "PPC_SIM01")
        replay_revision = str(replay_device["data"]["device"]["revision"])
        replay_expires = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
        request_id = f"r4e-i05-replay-{uuid4().hex}"
        replay_args = (
            "commander",
            "write",
            "PPC_SIM01",
            "agc_enable",
            "1",
            "--expected-revision",
            replay_revision,
            "--expires-at",
            replay_expires,
            "--request-id",
            request_id,
            "--yes",
        )
        first = await harness.cli_call(harness.cli_rt, *replay_args)
        replay = await harness.cli_call(harness.cli_rt, *replay_args)
        assert replay["data"]["command_id"] == first["data"]["command_id"]
        assert replay["data"]["request_id"] == request_id
        first_result = await harness.poll(
            harness.cli_rt,
            "commander",
            "result",
            first["data"]["command_id"],
            predicate=lambda item: item.get("state") == "SUCCEEDED",
            timeout=60,
        )
        assert first_result["outcome"] == "CONFIRMED_SUCCESS"

        pki = harness.plant / "commander-pki"
        deadline = Timestamp()
        deadline.FromDatetime(datetime.now(UTC) + timedelta(minutes=2))
        unauthorized_id = f"r4e-i05-unauthorized-{uuid4().hex}"
        async with mtls_channel(
            harness.endpoints["commander"], pki / "ca.crt", pki / "viewer.crt", pki / "viewer.key"
        ) as channel:
            with pytest.raises(grpc.aio.AioRpcError) as error:
                await cmd_rpc.CommanderServiceStub(channel).SubmitCommand(
                    cmd.SubmitCommandRequest(
                        device_id="PPC_SIM01",
                        point_id="agc_enable",
                        value=common.ScalarValue(uint64_value=0),
                        expires_at=deadline,
                        expected_revision=replay_revision,
                        request_id=unauthorized_id,
                    ),
                    timeout=10,
                )
            assert error.value.code() is grpc.StatusCode.PERMISSION_DENIED
        missing = await harness.cli_call(
            harness.cli_rt, "commander", "request-result", unauthorized_id, check=False
        )
        assert missing["error_code"] == "NOT_FOUND"

        while not audit_queue.empty():
            event = audit_queue.get_nowait()
            if event.get("event_type") == "ACCEPTED":
                command_ids.add(str(event["command_id"]))
        await harness.service("stop", "commander")
        commander_stopped = True
        before_unknown_ids = set(command_ids)
        unknown_client = AsyncModbusTcpClient("127.0.0.1", port=harness.modbus_ports["PPC_SIM01"])
        await unknown_client.connect()
        try:
            response = await unknown_client.write_registers(
                power_address, float_words(1666.0), device_id=1
            )
            assert not response.isError(), response
        finally:
            unknown_client.close()
        await asyncio.sleep(1.0)
        while not audit_queue.empty():
            event = audit_queue.get_nowait()
            if event.get("event_type") == "ACCEPTED":
                assert str(event["command_id"]) in before_unknown_ids
        rows = harness.read_recording(generation_id)
        assert not any(
            point.get("point_id") == "agc_power_setpoint" and number(point["value"]) == 1666.0
            for row in rows
            for point in row.get("points", [])
            if point.get("device_id") == "PPC_SIM01"
        )
        harness.record(
            "R4E-I05",
            generation_id=generation_id,
            fc06_address=enable_address,
            fc16_address=power_address,
            audit_command_ids=sorted(command_ids),
            commander_terminal="SUCCEEDED",
            dg_terminal="APPLIED",
            replay_command_id=replay["data"]["command_id"],
            unauthorized_status=error.value.code().name,
            unauthorized_lookup=missing["error_code"],
            unknown_dg_side_effect=False,
            unknown_new_audit=False,
        )
    finally:
        await subscription.unsubscribe()
        await client.drain()
        if commander_stopped:
            await harness.service("start", "commander")
        await _stop_generation(harness)


@pytest.mark.timeout(300)
async def test_i07_target_sink_mapping_release_and_lease_fencing(
    full_chain: FullChainHarness,
) -> None:
    harness = full_chain
    assert harness.sink is not None
    target = await start_target_gateway(harness)
    channels: list[grpc.aio.Channel] = []
    release_token = ""
    release_owner = ""
    source_release_token = ""
    source_owner = ""
    try:
        source_status = await harness.cli_call(harness.cli_rt, "service", "status", "gateway")
        target_status = await harness.cli_call(
            target.cli_config, "service", "status", target.service_name
        )
        source_pid = source_status["data"][0]["process"]["pid"]
        target_pid = target_status["data"][0]["process"]["pid"]
        assert source_pid != target_pid
        assert target.grpc_port != harness.ports["gateway"]
        assert target.modbus_port != harness.modbus_ports["WTG_SIM01"]
        source_before = await read_modbus_float(
            harness.modbus_ports["WTG_SIM01"], 400, holding=True
        )

        cert, key = target.pki["clients"]["lease-a"]
        channel_a = mtls_channel(target.endpoint, target.pki["ca"], cert, key)
        cert_b, key_b = target.pki["clients"]["lease-b"]
        channel_b = mtls_channel(target.endpoint, target.pki["ca"], cert_b, key_b)
        channels.extend((channel_a, channel_b))
        stubs = {
            "lease-a": gateway_rpc.IndustrialGatewayServiceStub(channel_a),
            "lease-b": gateway_rpc.IndustrialGatewayServiceStub(channel_b),
        }

        sample_id = f"r4e-i07-map-{uuid4().hex}"
        source_time = datetime.now(UTC)
        batch = fixed_batch(
            harness, "realtime", sample_id=sample_id, value=77.25, source_time=source_time
        )
        await publish_batch(harness, batch)
        mapped = await wait_gateway_value(
            harness, target.cli_config, "gw-wtg_sim01", SINK_TARGET_POINT, 77.25
        )
        assert utc_time(mapped["values"][0]["sample_timestamp"]) == source_time
        assert abs(await read_modbus_float(target.modbus_port, 400, holding=True) - 77.25) < 1e-3
        assert (
            abs(
                await read_modbus_float(harness.modbus_ports["WTG_SIM01"], 400, holding=True)
                - source_before
            )
            < 1e-3
        )

        source_cert, source_key = harness.pki["clients"]["cli"]
        source_channel = mtls_channel(
            harness.gw_grpc_endpoint, harness.pki["ca"], source_cert, source_key
        )
        channels.append(source_channel)
        source_stub = gateway_rpc.IndustrialGatewayServiceStub(source_channel)
        source_owner = f"r4e-i07-source-{uuid4().hex}"
        source_claim = await source_stub.ClaimTargets(
            gateway.ClaimTargetsRequest(
                gateway_id="gw-wtg_sim01",
                owner_id=source_owner,
                variable_ids=[SINK_TARGET_POINT],
                action="ACQUIRE",
                lease_ms=30000,
            ),
            timeout=10,
        )
        assert source_claim.accepted is True, source_claim
        source_release_token = source_claim.lease_token
        source_updates = await source_stub.UpdateVariables(
            gateway.UpdateVariablesRequest(
                gateway_id="gw-wtg_sim01",
                lease_token=source_claim.lease_token,
                values=[
                    gateway.VariableUpdate(variable_id=SINK_TARGET_POINT, value=encode_scalar(88.5))
                ],
            ),
            timeout=10,
        )
        assert len(source_updates.results) == 1 and source_updates.results[0].success is True
        target_after_source_write = await wait_gateway_value(
            harness, target.cli_config, "gw-wtg_sim01", SINK_TARGET_POINT, 77.25
        )
        source_release = await source_stub.ClaimTargets(
            gateway.ClaimTargetsRequest(
                gateway_id="gw-wtg_sim01",
                owner_id=source_owner,
                variable_ids=[SINK_TARGET_POINT],
                action="RELEASE",
                lease_token=source_claim.lease_token,
            ),
            timeout=10,
        )
        assert source_release.accepted is True
        source_release_token = ""

        busy_request = gateway.ClaimTargetsRequest(
            gateway_id="gw-wtg_sim01",
            owner_id="r4e-i07-before-release",
            variable_ids=[SINK_TARGET_POINT],
            action="ACQUIRE",
            lease_ms=30000,
        )
        busy = await stubs["lease-a"].ClaimTargets(busy_request, timeout=10)
        assert busy.accepted is False
        assert busy.error_code == "TARGET_CLAIMED"
        await harness.cli_call(harness.cli_rt, "sink", "stop", "gateway_output", "--yes")
        released = await stubs["lease-a"].ClaimTargets(busy_request, timeout=10)
        assert released.accepted is True, released
        release_token = released.lease_token
        release_owner = busy_request.owner_id
        cleanup_release = await stubs["lease-a"].ClaimTargets(
            gateway.ClaimTargetsRequest(
                gateway_id="gw-wtg_sim01",
                owner_id=release_owner,
                variable_ids=[SINK_TARGET_POINT],
                action="RELEASE",
                lease_token=release_token,
            ),
            timeout=10,
        )
        assert cleanup_release.accepted is True
        release_token = ""

        async def acquire(owner: str) -> gateway.ClaimTargetsResponse:
            return await stubs[owner].ClaimTargets(
                gateway.ClaimTargetsRequest(
                    gateway_id="gw-wtg_sim01",
                    owner_id=f"r4e-i07-{owner}",
                    variable_ids=[SINK_TARGET_POINT],
                    action="ACQUIRE",
                    lease_ms=1000,
                ),
                timeout=10,
            )

        results = await asyncio.gather(acquire("lease-a"), acquire("lease-b"))
        winners = [
            (owner, result)
            for owner, result in zip(("lease-a", "lease-b"), results, strict=False)
            if result.accepted
        ]
        losers = [
            (owner, result)
            for owner, result in zip(("lease-a", "lease-b"), results, strict=False)
            if not result.accepted
        ]
        assert len(winners) == 1 and len(losers) == 1
        assert losers[0][1].error_code == "TARGET_CLAIMED"
        winner, won = winners[0]
        loser = losers[0][0]
        release_token = won.lease_token
        release_owner = f"r4e-i07-{winner}"

        with pytest.raises(grpc.aio.AioRpcError) as loser_error:
            await stubs[loser].UpdateVariables(
                gateway.UpdateVariablesRequest(
                    gateway_id="gw-wtg_sim01",
                    lease_token=won.lease_token,
                    values=[
                        gateway.VariableUpdate(
                            variable_id=SINK_TARGET_POINT, value=encode_scalar(22.0)
                        )
                    ],
                ),
                timeout=10,
            )
        assert loser_error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        await asyncio.sleep(1.2)
        with pytest.raises(grpc.aio.AioRpcError) as stale_error:
            await stubs[winner].UpdateVariables(
                gateway.UpdateVariablesRequest(
                    gateway_id="gw-wtg_sim01",
                    lease_token=won.lease_token,
                    values=[
                        gateway.VariableUpdate(
                            variable_id=SINK_TARGET_POINT, value=encode_scalar(33.0)
                        )
                    ],
                ),
                timeout=10,
            )
        assert stale_error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
        stale_renew = await stubs[winner].ClaimTargets(
            gateway.ClaimTargetsRequest(
                gateway_id="gw-wtg_sim01",
                owner_id=release_owner,
                variable_ids=[SINK_TARGET_POINT],
                action="RENEW",
                lease_token=won.lease_token,
                lease_ms=1000,
            ),
            timeout=10,
        )
        assert stale_renew.accepted is False
        assert stale_renew.error_code == "LEASE_LOST"
        release_token = ""
        final = await harness.cli_call(
            target.cli_config, "gateway", "read", "gw-wtg_sim01", SINK_TARGET_POINT
        )
        assert abs(number(final["data"]["values"][0]["value"]) - 77.25) < 1e-3
        harness.record(
            "R4E-I07",
            source_pid=source_pid,
            target_pid=target_pid,
            source_grpc_port=harness.ports["gateway"],
            target_grpc_port=target.grpc_port,
            source_modbus_port=harness.modbus_ports["WTG_SIM01"],
            target_modbus_port=target.modbus_port,
            sink_mapping_sample_id=sample_id,
            sink_mapping_value=77.25,
            source_loopback_value_unchanged=source_before,
            source_independent_write=88.5,
            target_after_source_write=target_after_source_write["values"][0],
            busy_before_release=busy.error_code,
            acquire_after_release=released.accepted,
            concurrent_winners=1,
            loser_error=losers[0][1].error_code,
            loser_write=loser_error.value.code().name,
            stale_write=stale_error.value.code().name,
            stale_renew=stale_renew.error_code,
            lease_ms=1000,
        )
    finally:
        if source_release_token:
            source_cert, source_key = harness.pki["clients"]["cli"]
            async with mtls_channel(
                harness.gw_grpc_endpoint, harness.pki["ca"], source_cert, source_key
            ) as source_cleanup_channel:
                await gateway_rpc.IndustrialGatewayServiceStub(source_cleanup_channel).ClaimTargets(
                    gateway.ClaimTargetsRequest(
                        gateway_id="gw-wtg_sim01",
                        owner_id=source_owner,
                        variable_ids=[SINK_TARGET_POINT],
                        action="RELEASE",
                        lease_token=source_release_token,
                    ),
                    timeout=5,
                )
        if release_token:
            identity = "lease-b" if release_owner.endswith("lease-b") else "lease-a"
            cert, key = target.pki["clients"][identity]
            channel = mtls_channel(target.endpoint, target.pki["ca"], cert, key)
            try:
                await gateway_rpc.IndustrialGatewayServiceStub(channel).ClaimTargets(
                    gateway.ClaimTargetsRequest(
                        gateway_id="gw-wtg_sim01",
                        owner_id=release_owner,
                        variable_ids=[SINK_TARGET_POINT],
                        action="RELEASE",
                        lease_token=release_token,
                    ),
                    timeout=5,
                )
            finally:
                await channel.close()
        await asyncio.gather(*(channel.close() for channel in channels))
        await harness.cli_call(
            harness.cli_rt, "sink", "start", "gateway_output", "--yes", check=False
        )
        await stop_target_gateway(target)
