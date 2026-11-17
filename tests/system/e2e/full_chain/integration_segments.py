"""R4-E 分段验收的独立协议客户端与真实进程装配原语。"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import math
import struct
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
import nats
import yaml
from aiokafka import AIOKafkaConsumer
from nats.aio.msg import Msg

from cli.runtime import Runtime
from contracts.sample_batch import SampleBatch, SampleItem
from tests.cli.test_r1f3_strict_cli_e2e import _write_cli
from tests.support.gateway import reserve_port, write_mtls_pki

from .conftest import FARM, REPO_ROOT, SRC, FullChainHarness, _dump, _load, _write_gateway_service


def utc_time(raw: str) -> datetime:
    value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    assert value.tzinfo is not None
    return value.astimezone(UTC)


def number(raw: object) -> float:
    if isinstance(raw, bool):
        raise AssertionError(f"boolean is not a numeric sample: {raw!r}")
    if isinstance(raw, int | float):
        result = float(raw)
    elif isinstance(raw, dict):
        values = [
            raw.get(key) for key in ("double_value", "float_value", "int_value", "uint_value")
        ]
        candidates = [value for value in values if type(value) in (int, float)]
        assert len(candidates) == 1, raw
        result = float(candidates[0])
    else:
        raise AssertionError(f"invalid numeric sample: {raw!r}")
    assert math.isfinite(result)
    return result


def mtls_channel(endpoint: str, ca: Path, cert: Path, key: Path) -> grpc.aio.Channel:
    return grpc.aio.secure_channel(
        endpoint,
        grpc.ssl_channel_credentials(
            root_certificates=ca.read_bytes(),
            private_key=key.read_bytes(),
            certificate_chain=cert.read_bytes(),
        ),
    )


def generator_channel(harness: FullChainHarness) -> grpc.aio.Channel:
    cert, key = harness.pki["clients"]["generator"]
    return mtls_channel(harness.gw_grpc_endpoint, harness.pki["ca"], cert, key)


def fixed_batch(
    harness: FullChainHarness,
    mode: str,
    *,
    sample_id: str,
    value: float,
    source_time: datetime,
    device_id: str = "WTG_SIM01",
    point_id: str = "wind_speed",
) -> SampleBatch:
    collector = harness.rt_collector if mode == "realtime" else harness.reliable_collector
    suffix = uuid4().hex
    return SampleBatch(
        schema_version=2,
        batch_id=f"r4e-{mode}-{suffix}",
        mode=mode,  # type: ignore[arg-type]
        wind_farm_id=FARM,
        collector_id=collector,
        task_id=f"r4e-fixed-{mode}",
        device_id=device_id,
        publish_time=datetime.now(UTC),
        samples=(
            SampleItem(
                point_id=point_id,
                sample_id=sample_id,
                value=value,
                quality="good",
                sample_time=source_time,
                receive_time=source_time,
                max_staleness_ms=60000 if mode == "realtime" else None,
            ),
        ),
    )


async def publish_batch(harness: FullChainHarness, batch: SampleBatch) -> Any:
    client = await nats.connect(harness.stack.nats, connect_timeout=10)
    try:
        if batch.mode == "reliable":
            ack = await client.jetstream().publish(batch.subject(), batch.encode(), timeout=10)
            assert ack.stream == harness.sink.history_stream if harness.sink is not None else False
            assert ack.seq > 0
            return ack
        await client.publish(batch.subject(), batch.encode())
        await client.flush(timeout=10)
        return None
    finally:
        await client.drain()


async def kafka_consumer(harness: FullChainHarness, label: str) -> AIOKafkaConsumer:
    assert harness.sink is not None
    consumer = AIOKafkaConsumer(
        harness.sink.kafka_topic,
        bootstrap_servers=harness.stack.kafka,
        auto_offset_reset="earliest",
        group_id=f"r4e-{label}-{uuid4().hex}",
        enable_auto_commit=False,
    )
    await consumer.start()
    return consumer


async def wait_kafka_sample(
    consumer: AIOKafkaConsumer, sample_id: str, timeout: float = 60
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = await consumer.getmany(timeout_ms=1000, max_records=500)
        for partition_records in records.values():
            for record in partition_records:
                item = json.loads(record.value)
                if item.get("sample_id") == sample_id:
                    return item
    raise AssertionError(f"Kafka sample not observed: {sample_id}")


async def wait_gateway_value(
    harness: FullChainHarness,
    config: Path,
    gateway_id: str,
    point_id: str,
    expected: float,
    timeout: float = 60,
) -> dict[str, Any]:
    return await harness.poll(
        config,
        "gateway",
        "read",
        gateway_id,
        point_id,
        predicate=lambda data: bool(data.get("values"))
        and data["values"][0].get("value") is not None
        and abs(number(data["values"][0]["value"]) - expected) <= 1e-3,
        timeout=timeout,
    )


def correlate_source(
    rows: list[dict[str, Any]], device_id: str, point_id: str, sample_time: datetime, value: float
) -> dict[str, Any]:
    candidates = [
        (row, point)
        for row in rows
        for point in row.get("points", [])
        if point.get("device_id") == device_id
        and point.get("point_id") == point_id
        and utc_time(point["source_time"]) <= sample_time
    ]
    assert candidates, (device_id, point_id, sample_time)
    row, point = max(candidates, key=lambda item: utc_time(item[1]["source_time"]))
    delta = (sample_time - utc_time(point["source_time"])).total_seconds()
    assert 0 <= delta <= 1.0, delta
    source_value = number(point["value"])
    assert abs(value - source_value) <= max(1e-3, abs(source_value) * 2e-3)
    return {"generation_id": row["generation_id"], "sequence": row["sequence"], **point}


@dataclass(slots=True)
class ObservedBatch:
    batch: SampleBatch
    js_sequence: int | None


class DualBatchObserver:
    """在两个精确 Subject 上预订阅，独立于 Collector/Sink 消费链。"""

    def __init__(self, harness: FullChainHarness, device_id: str) -> None:
        self.harness = harness
        self.rt_subject = f"wh.rt.batch.{FARM}.{harness.rt_collector}.{device_id}"
        self.hist_subject = f"wh.hist.batch.{FARM}.{harness.reliable_collector}.{device_id}"
        self.queues: dict[str, asyncio.Queue[ObservedBatch]] = {
            "realtime": asyncio.Queue(),
            "reliable": asyncio.Queue(),
        }
        self.client: Any = None
        self.subscriptions: list[Any] = []

    async def start(self) -> None:
        self.client = await nats.connect(self.harness.stack.nats, connect_timeout=10)

        async def on_rt(message: Msg) -> None:
            batch = SampleBatch.decode(message.data, "realtime", subject=message.subject)
            await self.queues["realtime"].put(ObservedBatch(batch, None))

        async def on_hist(message: Msg) -> None:
            batch = SampleBatch.decode(message.data, "reliable", subject=message.subject)
            sequence = int(message.metadata.sequence.stream)
            await message.ack()
            await self.queues["reliable"].put(ObservedBatch(batch, sequence))

        self.subscriptions = [
            await self.client.subscribe(self.rt_subject, cb=on_rt),
            await self.client.jetstream().subscribe(self.hist_subject, cb=on_hist, manual_ack=True),
        ]
        await self.client.flush(timeout=10)

    async def wait(self, mode: str, count: int, timeout: float = 60) -> list[ObservedBatch]:
        result: list[ObservedBatch] = []
        for _ in range(count):
            result.append(await asyncio.wait_for(self.queues[mode].get(), timeout=timeout))
        return result

    async def close(self) -> None:
        for subscription in self.subscriptions:
            with contextlib.suppress(Exception):
                await subscription.unsubscribe()
        if self.client is not None:
            await self.client.drain()


@dataclass(slots=True)
class TargetGateway:
    harness: FullChainHarness
    root: Path
    service_name: str
    endpoint: str
    grpc_port: int
    modbus_port: int
    pki: dict[str, Any]
    cli_config: Path
    original_sinks: bytes
    original_cli_rt: bytes
    original_cli_rel: bytes
    original_sink_process: dict[str, Any]
    sinks_path: Path
    started: bool = False


async def start_target_gateway(harness: FullChainHarness) -> TargetGateway:
    """启动独立目标 Gateway，并临时把一路 Sink 映射切换到该目标。"""
    assert harness.sink is not None
    root = harness.root / "r4e-target-gateway"
    plant = root / "plant"
    import shutil

    shutil.copytree(harness.plant, plant)
    pki = write_mtls_pki(
        plant / "pki", ("cli", "gateway", "generator", "sink", "commander", "lease-a", "lease-b")
    )
    grpc_port = reserve_port()
    modbus_port = reserve_port()
    service_path = _write_gateway_service(plant, grpc_port=grpc_port, pki=pki)
    document = _load(service_path)
    document["grpc"]["permissions"].update(
        {"lease-a": ["inject", "read"], "lease-b": ["inject", "read"]}
    )
    servers = [item for item in document["servers"] if item["device_id"] == "WTG_SIM01"]
    assert len(servers) == 1
    servers[0]["server_id"] = "gw-wtg_sim01"
    servers[0]["listen_host"] = "127.0.0.1"
    servers[0]["listen_port"] = modbus_port
    servers[0]["control"] = {
        "enabled": False,
        "target": {"kind": "none"},
        "mappings": [],
    }
    document["servers"] = servers
    _dump(service_path, document)

    service_name = "gateway_r4e_target"
    harness.cli.register_process(
        service_name,
        [
            sys.executable,
            "-m",
            "industrial_gateway.main",
            "--config",
            str(service_path),
            "--project-root",
            str(plant),
        ],
        cwd=plant,
    )
    endpoint = f"127.0.0.1:{grpc_port}"
    endpoints = dict(harness.endpoints)
    endpoints["gateway"] = endpoint
    cli_config = root / "cli.yaml"
    _write_cli(
        cli_config,
        collector="collector_realtime",
        endpoints=endpoints,
        processes=harness.cli.processes,
        gateway_pki=pki,
        commander_pki=type("Pki", (), {"tmp": harness.plant / "commander-pki"})(),
        sink_bundle=harness.sink.bundle,
    )
    cli_document = _load(cli_config)
    cli_document["services"][service_name] = {
        "grpc_endpoint": endpoint,
        "tls_profile": "gateway",
        "manager": "process",
        "target": service_name,
        "process": harness.cli.processes[service_name],
    }
    _dump(cli_config, cli_document)

    sinks_path = harness.sink.root / "configs" / "instances" / "sinks.yaml"
    target = TargetGateway(
        harness=harness,
        root=root,
        service_name=service_name,
        endpoint=endpoint,
        grpc_port=grpc_port,
        modbus_port=modbus_port,
        pki=pki,
        cli_config=cli_config,
        original_sinks=sinks_path.read_bytes(),
        original_cli_rt=harness.cli_rt.read_bytes(),
        original_cli_rel=harness.cli_rel.read_bytes(),
        original_sink_process=copy.deepcopy(harness.cli.processes["sink"]),
        sinks_path=sinks_path,
    )
    try:
        started = await harness.cli.service(cli_config, "start", service_name)
        target.started = True
        assert started["data"]["process"]["confirmed"] is True
        await harness.poll(
            cli_config,
            "gateway",
            "list",
            predicate=lambda data: any(
                item.get("gateway_id") == "gw-wtg_sim01" for item in data.get("gateways", [])
            ),
            timeout=60,
        )

        sinks = _load(sinks_path)
        connection = sinks["sinks"]["gateway_sink"]["connection"]
        connection["endpoint"] = endpoint
        connection["gateway_id"] = "gw-wtg_sim01"
        _dump(sinks_path, sinks)
        sink_cert, sink_key = pki["clients"]["sink"]
        harness.cli.processes["sink"]["env"]["WH_FULLCHAIN_GATEWAY_TLS"] = json.dumps(
            {
                "ca_pem": pki["ca"].read_text(encoding="utf-8"),
                "client_cert_pem": sink_cert.read_text(encoding="utf-8"),
                "client_key_pem": sink_key.read_text(encoding="utf-8"),
            }
        )
        _write_cli(
            harness.cli_rt,
            collector="collector_realtime",
            endpoints=harness.endpoints,
            processes=harness.cli.processes,
            gateway_pki=harness.pki,
            commander_pki=type("Pki", (), {"tmp": harness.plant / "commander-pki"})(),
            sink_bundle=harness.sink.bundle,
        )
        _write_cli(
            harness.cli_rel,
            collector="collector_reliable",
            endpoints=harness.endpoints,
            processes=harness.cli.processes,
            gateway_pki=harness.pki,
            commander_pki=type("Pki", (), {"tmp": harness.plant / "commander-pki"})(),
            sink_bundle=harness.sink.bundle,
        )
        await harness.service("restart", "sink")
        return target
    except BaseException:
        await stop_target_gateway(target)
        raise


async def stop_target_gateway(target: TargetGateway) -> None:
    harness = target.harness
    target.sinks_path.write_bytes(target.original_sinks)
    harness.cli.processes["sink"] = target.original_sink_process
    harness.cli_rt.write_bytes(target.original_cli_rt)
    harness.cli_rel.write_bytes(target.original_cli_rel)
    with contextlib.suppress(Exception):
        await harness.cli.service(target.cli_config, "stop", target.service_name)
    with contextlib.suppress(Exception):
        await harness.service("restart", "sink")


async def read_modbus_float(port: int, address: int, *, holding: bool) -> float:
    from pymodbus.client import AsyncModbusTcpClient

    client = AsyncModbusTcpClient("127.0.0.1", port=port)
    await client.connect()
    try:
        if holding:
            response = await client.read_holding_registers(address, count=2, device_id=1)
        else:
            response = await client.read_input_registers(address, count=2, device_id=1)
        assert not response.isError(), response
        return struct.unpack(">f", struct.pack(">HH", *response.registers))[0]
    finally:
        client.close()


def float_words(value: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", value)))


def load_yaml(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


__all__ = [
    "DualBatchObserver",
    "FullChainHarness",
    "REPO_ROOT",
    "SRC",
    "Runtime",
    "TargetGateway",
    "correlate_source",
    "fixed_batch",
    "float_words",
    "generator_channel",
    "kafka_consumer",
    "load_yaml",
    "mtls_channel",
    "number",
    "publish_batch",
    "read_modbus_float",
    "start_target_gateway",
    "stop_target_gateway",
    "utc_time",
    "wait_gateway_value",
    "wait_kafka_sample",
]
