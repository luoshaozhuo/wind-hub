"""Round 5 真实服务链路测试共享 fixture。

所有断言发生在系统边界：真实 Docker 服务（独立 compose project）、真实
``wind-hub-sink-service`` / ``wind-hub-collector`` 子进程、真实网络协议；
任何内存替身都不允许冒充。docker 不可用时整个 r5 包明确 skip。
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import nats
import pytest

from contracts.sample_message import DeliveryMode, SampleMessage
from sink_service.management_cli import request as management_request
from sink_service.nats_init import ensure_history_stream
from tests.fixtures.services.r5_stack import R5Stack, docker_available, down_stack, up_stack
from tests.support.process import CollectorProcess, free_port, start_sink_service
from tests.support.wait import wait_tcp_port, wait_until

SITE_ID = "test"
DEVICE_ID = "modbus-1"


@pytest.fixture(scope="session")
def r5_stack() -> AsyncIterator[R5Stack]:
    """session 级真实服务栈：NATS/Redis/TDengine/Kafka，结束只清理本 project。"""
    if not docker_available():
        pytest.skip("SKIPPED: docker unavailable — R5 real-service suite cannot run")
    stack = up_stack()
    asyncio.run(ensure_history_stream((stack.nats,)))
    try:
        yield stack
    finally:
        down_stack(stack)


@pytest.fixture
async def r5_sink_factory(
    tmp_path: Path,
    r5_stack: R5Stack,
) -> AsyncIterator[Callable[..., Awaitable[CollectorProcess]]]:
    """启动指向 R5 NATS 的真实 sink_service 子进程。"""
    started: list[CollectorProcess] = []

    async def _start(
        config_dir: Path,
        *,
        delivery: str = "realtime",
        realtime_sinks: tuple[str, ...] = (),
        reliable_sinks: tuple[str, ...] = (),
        deployment_id: str | None = None,
    ) -> CollectorProcess:
        state_dir = tmp_path / f"r5-state-{free_port()}"
        state_dir.mkdir(parents=True, exist_ok=True)
        proc = start_sink_service(
            config_dir,
            nats_servers=(r5_stack.nats,),
            wind_farm_id=SITE_ID,
            deployment_id=deployment_id or f"r5-{uuid.uuid4().hex[:10]}",
            delivery=delivery,
            realtime_sinks=realtime_sinks,
            reliable_sinks=reliable_sinks,
            state_dir=state_dir,
            log_dir=tmp_path,
        )
        started.append(proc)
        await wait_tcp_port("127.0.0.1", int(proc.grpc_target.rpartition(":")[2]))
        if not proc.is_running():
            raise RuntimeError(f"sink_service exited early; see {proc.log_path}")
        return proc

    yield _start

    for proc in started:
        try:
            if proc.is_running():
                proc.terminate()
        except BaseException:
            proc.kill_tree()
        finally:
            proc.close_log()


def make_sample(
    point: str,
    value: float | int | bool | str,
    *,
    task: str = "modbus-telemetry",
    collector: str = "collector-r5",
    device: str = DEVICE_ID,
    sample_id: str | None = None,
    sample_time: datetime | None = None,
    max_staleness_ms: int | None = 60_000,
    quality: str = "good",
) -> SampleMessage:
    """构造生产信封 SampleMessage（与 Collector 发布的线路格式一致）。"""
    now = sample_time or datetime.now(UTC)
    return SampleMessage(
        wind_farm_id=SITE_ID,
        collector_id=collector,
        task_id=task,
        device_id=device,
        point_id=point,
        sample_id=sample_id or f"{collector}:{uuid.uuid4().hex[:12]}:{point}",
        value=value,
        quality=quality,
        sample_time=now,
        publish_time=datetime.now(UTC),
        max_staleness_ms=max_staleness_ms,
    )


async def publish(stack: R5Stack, sample: SampleMessage, mode: DeliveryMode) -> None:
    """通过真实 NATS 连接发布（reliable 走 JetStream，realtime 走 Core）。"""
    client = await nats.connect(servers=stack.nats, connect_timeout=3.0)
    try:
        payload = sample.encode()
        if mode == "reliable":
            await client.jetstream().publish(sample.subject("reliable"), payload)
        else:
            await client.publish(sample.subject("realtime"), payload)
    finally:
        await client.close()


async def mgmt(proc: CollectorProcess, operation: str, name: str | None = None) -> dict[str, Any]:
    """调用真实 sink_service 管理面 gRPC（windhub.sink.v1.SinkService）。"""
    return await management_request(proc.grpc_target, operation, name)


async def wait_state(
    proc: CollectorProcess,
    name: str,
    state: str,
    *,
    timeout: float = 20.0,
) -> dict[str, Any]:
    async def _match() -> dict[str, Any] | None:
        status = await mgmt(proc, "GetSinkStatus", name)
        return status if status.get("state") == state else None

    return await wait_until(
        _match, timeout=timeout, interval=0.5, description=f"sink {name} {state}"
    )
