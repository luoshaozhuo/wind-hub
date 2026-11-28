"""Eight-service CLI-driven real E2E smoke on the active e2e-final deployment.

Chain under test: Data Generator -> Gateway -> both Collectors -> NATS/JetStream
-> Sink -> Kafka -> Storage -> TDengine.  Every business process is started by
the formal CLI process manager; middleware is the isolated R5 Docker stack.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import grpc
import pytest
from aiokafka import AIOKafkaConsumer, TopicPartition

from contracts import common_pb2
from contracts import common_pb2_grpc as common_rpc
from tests.system.e2e.eight_service.conftest import (
    SERVICES,
    EightServiceHarness,
)

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.end_to_end,
]

DELIVERIES = ("e2e_kafka_protocol", "e2e_redis_power", "e2e_gateway_power", "e2e_modbus_power")
STORAGE_PORT = 52110


def _storage_port(harness: EightServiceHarness) -> int:
    import yaml

    config = yaml.safe_load(
        (harness.root / "configs" / "services" / "service_storage-e2e_v2.0.1.yaml").read_text(
            encoding="utf-8"
        )
    )
    return int(config["service"]["grpc"]["listen_port"])


async def _kafka_offsets(bootstrap: str, topic: str) -> dict[str, int]:
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=bootstrap,
        group_id=None,
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        partitions = consumer.partitions_for_topic(topic)
        if not partitions:
            await consumer.getmany(timeout_ms=1000)
            partitions = consumer.partitions_for_topic(topic)
        assert partitions, f"topic {topic} has no partitions"
        topic_partitions = [TopicPartition(topic, partition) for partition in sorted(partitions)]
        begins = await consumer.beginning_offsets(topic_partitions)
        ends = await consumer.end_offsets(topic_partitions)
        total = sum(ends[tp] - begins[tp] for tp in topic_partitions)
        return {"total": total}
    finally:
        await consumer.stop()


def _query_all_groups(harness: EightServiceHarness, *arguments: str) -> list[dict[str, object]]:
    """累积签名游标的全部页面，不以第一页代替完整历史响应。"""
    groups: list[dict[str, object]] = []
    page_token = ""
    seen_tokens: set[str] = set()
    for _ in range(1000):
        page_arguments = list(arguments)
        if page_token:
            page_arguments.extend(("--page-token", page_token))
        response = harness.cli(*page_arguments, check=False, timeout=60)
        assert response.get("status") == "SUCCESS", response
        data = response.get("data") or {}
        page_groups = data.get("groups")
        assert isinstance(page_groups, list), response
        groups.extend(page_groups)
        next_token = data.get("next_page_token") or ""
        complete = data.get("complete")
        assert isinstance(complete, bool), response
        if not next_token:
            assert complete, response
            return groups
        assert not complete and next_token not in seen_tokens, response
        seen_tokens.add(next_token)
        page_token = next_token
    raise AssertionError("Storage grouped query exceeded 1000 pages")


def _tdengine_row_count(harness: EightServiceHarness) -> dict[str, object]:
    databases = harness.stack.tdengine_sql("SHOW DATABASES")["data"]
    selected = None
    tables: list[list[object]] = []
    for row in databases:
        name = str(row[0])
        if name in {"information_schema", "performance_schema", "log"} or name.startswith(
            "wind_hub_r5"
        ):
            continue
        probe = harness.stack.tdengine_sql(f"SHOW `{name}`.STABLES", database=name)["data"]
        if probe and any(str(item[0]).startswith("st_") for item in probe):
            selected = name
            tables = probe
            break
    assert selected is not None, f"no Storage TDengine database in {databases!r}"
    stable = str(tables[0][0])
    rows = harness.stack.tdengine_sql(
        f"SELECT COUNT(*) FROM `{selected}`.`{stable}`", database=selected
    )["data"]
    return {"database": selected, "stable": stable, "count": rows[0][0]}


def _generator_truth_files(root: Path) -> list[Path]:
    directory = root / "artifacts" / "e2e-final" / "data-generator"
    if not directory.is_dir():
        return []
    return sorted(directory.rglob("*.jsonl"))


def test_eight_service_chain_is_cli_managed_and_persisted(
    started: EightServiceHarness,
) -> None:
    harness = started

    print("STEP1 status")
    # 1) Every configured process is observable through the formal CLI.
    for name in SERVICES + ("configuration",):
        status = harness.status(name)
        assert status["status"] == "SUCCESS", (name, status)

    print("STEP2 deliveries")
    # 2) Business operations are issued through CLI: deliveries then generation.
    listed = harness.cli("sink", "deliveries")
    assert listed["status"] == "SUCCESS", listed
    available = (listed.get("data") or {}).get("deliveries") or []
    available_ids = {item.get("delivery_id") or item.get("id") for item in available}
    assert set(DELIVERIES) <= available_ids, f"missing four-output Delivery: {available_ids}"
    for delivery in DELIVERIES:
        deadline = time.monotonic() + 45
        last: dict[str, object] = {}
        while time.monotonic() < deadline:
            last = harness.cli("sink", "start", delivery, "--yes", check=False, timeout=90)
            if last["status"] == "SUCCESS":
                break
            time.sleep(1.0)
        assert last.get("status") == "SUCCESS", (delivery, last)

    harvest = harness.cli("data-generator", "start", "e2e-plant-physical", "--yes", timeout=90)
    assert harvest["status"] == "SUCCESS", harvest

    print("STEP3 samples")
    # 3) Wait for PROTOCOL samples to reach Storage and TDengine.
    window_end = datetime.now(UTC) + timedelta(seconds=5)
    window_start = window_end - timedelta(minutes=10)
    proto_args = (
        "storage",
        "samples",
        "--wind-farm-id",
        "e2e-final",
        "--start-time",
        (window_start - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--end-time",
        window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--data-level",
        "protocol",
        "--device-id",
        "WTG_SIM01",
        "--page-size",
        "200",
    )
    deadline = time.monotonic() + 90
    sample_count = 0
    last: dict[str, object] = {}
    groups: list[dict[str, object]] = []
    while time.monotonic() < deadline:
        try:
            groups = _query_all_groups(harness, *proto_args)
            sample_count = sum(len(group.get("rows") or []) for group in groups)
        except AssertionError as error:
            last = {"query_error": str(error)}
        if sample_count:
            break
        time.sleep(1.0)
    if not sample_count:
        diagnostics = {
            name: harness.status(name)
            for name in (
                "data-generator",
                "collector-realtime",
                "collector-reliable",
                "sink",
                "storage",
            )
        }
        diagnostics["deliveries"] = harness.cli("sink", "deliveries", check=False)
        diagnostics["sink_metrics"] = harness.cli("sink", "metrics", check=False)
        diagnostics["storage"] = last
        raise AssertionError(f"Storage returned no PROTOCOL samples: {diagnostics}")

    print("STEP4 truth")
    # 4) Independent truth: TDengine rows, Kafka offsets and Generator recordings.
    deadline = time.monotonic() + 90
    tdengine: dict[str, object] = {}
    while time.monotonic() < deadline:
        try:
            tdengine = _tdengine_row_count(harness)
        except AssertionError:
            tdengine = {}
        if int(tdengine.get("count", 0)) > 0:
            break
        time.sleep(1.0)
    assert int(tdengine.get("count", 0)) > 0, f"TDengine persisted no rows: {tdengine}"
    truth_files = _generator_truth_files(harness.root)
    assert truth_files, "Data Generator produced no truth JSONL recording"
    assert all(path.stat().st_size > 0 for path in truth_files)

    print("STEP5 kafka")
    # 5) Kafka confirmation independent of Sink's internal state.
    offsets = asyncio.run(
        asyncio.wait_for(_kafka_offsets(harness.stack.kafka, "windhub.e2e.history"), timeout=30)
    )
    assert offsets["total"] > 0, offsets

    print("STEP6 auth")
    # 6) Authentication boundary: an anonymous Storage call is UNAUTHENTICATED.
    port = _storage_port(harness)

    async def _anonymous() -> int:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as channel:
            stub = common_rpc.CommonServiceStub(channel)
            try:
                await stub.GetServiceStatus(common_pb2.Empty(), timeout=5)
            except grpc.aio.AioRpcError as error:
                return error.code().value[0]
        return 0

    assert asyncio.run(_anonymous()) == grpc.StatusCode.UNAUTHENTICATED.value[0]

    print("STEP7 restart")
    # 7) Restart Storage through CLI and re-query the same historical window.
    harness.cli("service", "restart", "storage", "--yes", timeout=120)
    harness.wait_targets("storage")
    replay_deadline = time.monotonic() + 90
    replay_count = 0
    replay_error: AssertionError | None = None
    while time.monotonic() < replay_deadline:
        try:
            replay_groups = _query_all_groups(harness, *proto_args)
            replay_count = sum(len(group.get("rows") or []) for group in replay_groups)
            if replay_count:
                break
        except AssertionError as error:
            replay_error = error
        time.sleep(1)
    assert replay_count, (
        "historical PROTOCOL grouped query did not recover after Storage restart: "
        f"{replay_error}"
    )

    evidence = {
        "services": list(SERVICES),
        "protocol_samples": sample_count,
        "kafka_offsets": offsets,
        "tdengine": tdengine,
        "generator_truth_files": [str(path.relative_to(harness.root)) for path in truth_files],
        "replay_after_restart": replay_count,
    }
    (harness.root / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(evidence, ensure_ascii=False))
