"""Sink Service v1_21 §14 系统测试夹具：真实三进程 + 真实消息/目标基础设施。

所有系统用例只在进程与协议边界观察：

- 真实 Docker Compose 一次性 R5 栈（NATS JetStream / Redis / Kafka /
  TDengine），独立 project、动态 loopback 端口，故障注入只作用于本 project
  容器（见 :mod:`tests.fixtures.services.r5_stack`），绝不使用 Mock 冒充
  NATS/JetStream 或服务进程；
- 真实 ``wind-hub-sink-service`` 子进程（Main + realtime/reliable 两个
  spawn worker），mTLS gRPC 控制面，配置根严格走生产校验器接受的
  canonical 文档树（复用阶段一夹具）；
- 输入侧用独立 NATS 连接发布冻结的 SampleBatch v2，目标侧用独立 Redis/Kafka
  客户端做交付确认，不读取服务进程内部状态。

Docker 不可用时整个模块 skip（NOT_RUN 的环境口径），绝不退化为内存替身。
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import grpc
import nats
import pytest

from contracts import common_pb2, common_pb2_grpc, sink_service_pb2, sink_service_pb2_grpc
from contracts.sample_batch import SampleBatch, SampleItem
from tests.fixtures.services import r5_stack
from tests.sink_service._stage1_fixtures import (
    DELIVERIES_INSTANCE,
    SERVICE_FILE,
    SINKS_INSTANCE,
    service_document,
    template_doc,
    template_path,
    write_tree,
)
from tests.sink_service._stage1_tls import write_mtls_bundle
from tests.support.process import CollectorProcess, start_sink_service
from tests.support.wait import wait_tcp_port

#: 系统测试固定身份（与 §14 追溯矩阵中的 farm-a / collector-1 对齐）。
WIND_FARM_ID = "farm-a"
COLLECTOR_ID = "collector-1"
DEVICE_ID = "WT001"
REALTIME_DELIVERY = "ems_power"
RELIABLE_DELIVERY = "power_history"
REDIS_SINK = "ems_redis"
KAFKA_SINK = "history_kafka"
DEFAULT_STALENESS_MS = 60_000


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="module")
def qa_stack() -> r5_stack.R5Stack:
    """一次性真实服务栈；Docker 缺失时明确 skip（不以 Mock 顶替）。"""
    if not r5_stack.docker_available():
        pytest.skip("NOT_RUN: docker daemon unavailable for real service stack")
    stack = r5_stack.up_stack()
    try:
        yield stack
    finally:
        r5_stack.down_stack(stack)


@dataclass
class SinkSystemHarness:
    """一次系统用例独占的配置根、进程句柄与真实连接参数。"""

    root: Path
    stack: r5_stack.R5Stack
    grpc_port: int
    namespace: str
    kafka_topic: str
    history_stream: str
    dlq_stream: str
    dlq_subject: str
    token: str
    bundle: dict[str, Path]
    staleness_ms: int = DEFAULT_STALENESS_MS
    process: CollectorProcess | None = field(default=None)
    _nc: Any = field(default=None, repr=False)
    _streams_ready: bool = field(default=False, repr=False)

    # ---- 配置根 -------------------------------------------------------

    def write_dual_channel_root(
        self,
        *,
        reliable: bool = True,
        kafka_bootstrap: str | None = None,
        redis_address: str | None = None,
        nats_url: str | None = None,
        ack_wait_seconds: int | None = None,
        callers: list[dict[str, Any]] | None = None,
    ) -> Path:
        """写出 realtime(Redis) + 可选 reliable(Kafka) 的 canonical 配置根。

        ``nats_url`` 允许把服务的 NATS 指向故障代理（S3-03 线协议注入）；
        ``ack_wait_seconds`` 缩短 JetStream 重投等待，控制故障用例时长；
        ``callers`` 允许授权矩阵用例注入 query-only 等受限调用方。
        """
        nats_url = nats_url or self.stack.nats
        redis_address = redis_address or self.stack.redis
        kafka_bootstrap = kafka_bootstrap or self.stack.kafka
        redis_host, _, redis_port = redis_address.rpartition(":")
        self.root.mkdir(parents=True, exist_ok=True)
        write_mtls_bundle(self.root)

        service = service_document(
            port=self.grpc_port,
            config_root=str(self.root),
            callers=callers
            or [
                {
                    "caller_id": "maintenance",
                    "certificate_identity": "spiffe://wind-hub/maintenance",
                    "permissions": ["query", "control", "config"],
                }
            ],
        )
        service["wind_farm_id"] = WIND_FARM_ID
        service["deployment_id"] = f"qa-{self.token}"
        service["nats"]["servers"] = [nats_url]
        service["nats"]["reliable_stream"] = self.history_stream
        service["nats"]["dead_letter_stream"] = self.dlq_stream
        service["nats"]["dead_letter_subject"] = self.dlq_subject
        if ack_wait_seconds is not None:
            service["reliable"]["ack_wait_seconds"] = ack_wait_seconds

        redis_template = template_doc("redis_realtime")
        redis_template["connection"] = {"host": redis_host, "port": int(redis_port)}
        redis_template["output"]["namespace"] = self.namespace

        kafka_template = template_doc("kafka_history")
        kafka_template["connection"]["bootstrap_servers"] = [kafka_bootstrap]
        kafka_template["output"]["topic"] = self.kafka_topic

        realtime_template = template_doc("realtime_power")
        realtime_template["staleness"] = {"max_age_ms": self.staleness_ms}

        docs: dict[str, dict[str, Any]] = {
            SERVICE_FILE: service,
            template_path("redis_realtime"): redis_template,
            template_path("realtime_power"): realtime_template,
            SINKS_INSTANCE: {
                "schema_version": 1,
                "file_version": "1.0.0",
                "revision": "system-sinks-r1",
                "sinks": {
                    REDIS_SINK: {
                        "template_id": "redis_realtime",
                        "template_version": "1.0.0",
                        "connection": {"host": redis_host, "port": int(redis_port)},
                        "overrides": {},
                    }
                },
            },
            DELIVERIES_INSTANCE: {
                "schema_version": 1,
                "file_version": "1.0.0",
                "revision": "system-deliveries-r1",
                "deliveries": {
                    REALTIME_DELIVERY: {
                        "template_id": "realtime_power",
                        "template_version": "1.0.0",
                        "sink_id": REDIS_SINK,
                        "desired_state": "START",
                        "source": {
                            "wind_farm_id": WIND_FARM_ID,
                            "collector_id": COLLECTOR_ID,
                            "devices": [],
                            "points": [],
                        },
                        "overrides": {},
                    }
                },
            },
        }
        if reliable:
            reliable_template = template_doc("reliable_history")
            reliable_template["reliable"]["dead_letter_subject"] = self.dlq_subject
            docs[template_path("kafka_history")] = kafka_template
            docs[template_path("reliable_history")] = reliable_template
            docs[SINKS_INSTANCE]["sinks"][KAFKA_SINK] = {
                "template_id": "kafka_history",
                "template_version": "1.0.0",
                "connection": {"bootstrap_servers": [kafka_bootstrap]},
                "overrides": {},
            }
            docs[DELIVERIES_INSTANCE]["deliveries"][RELIABLE_DELIVERY] = {
                "template_id": "reliable_history",
                "template_version": "1.0.0",
                "sink_id": KAFKA_SINK,
                "desired_state": "START",
                "source": {
                    "wind_farm_id": WIND_FARM_ID,
                    "collector_id": COLLECTOR_ID,
                    "devices": [],
                    "points": [],
                },
                "overrides": {},
            }
        write_tree(self.root, docs)
        return self.root

    # ---- JetStream ----------------------------------------------------

    async def provision_streams(self, *, dlq_max_msgs: int = -1) -> None:
        """生产运行时只 stream_info 不建流；系统测试负责预置真实 Stream。"""
        from nats.js.api import DiscardPolicy, RetentionPolicy, StorageType, StreamConfig

        client = await nats.connect(servers=self.stack.nats, connect_timeout=10)
        try:
            js = client.jetstream()
            existing = {info.config.name for info in await js.streams_info()}
            if self.history_stream not in existing:
                await js.add_stream(
                    StreamConfig(
                        name=self.history_stream,
                        subjects=["wh.hist.batch.>"],
                        storage=StorageType.FILE,
                        retention=RetentionPolicy.LIMITS,
                        discard=DiscardPolicy.NEW,
                        max_bytes=256 * 1024 * 1024,
                        max_age=7200,
                    )
                )
            if self.dlq_stream not in existing:
                await js.add_stream(
                    # 生产运行时强制 DLQ 容量 >= service.dlq_max_bytes(1GiB)、
                    # max_age >= 86400s（reliable_runtime 启动校验）；配额用例
                    # 另通过 max_msgs 收紧消息数。
                    StreamConfig(
                        name=self.dlq_stream,
                        subjects=[self.dlq_subject],
                        storage=StorageType.FILE,
                        retention=RetentionPolicy.LIMITS,
                        discard=DiscardPolicy.NEW,
                        max_bytes=1024 * 1024 * 1024,
                        max_msgs=dlq_max_msgs if dlq_max_msgs > 0 else -1,
                        max_age=86400,
                    )
                )
            self._streams_ready = True
        finally:
            await client.drain()

    async def delete_streams(self) -> None:
        """清理本 harness 自建 Stream，避免通配 subject 跨用例重叠。"""
        if not self._streams_ready:
            return
        client = await nats.connect(servers=self.stack.nats, connect_timeout=10)
        try:
            js = client.jetstream()
            for name in (self.history_stream, self.dlq_stream):
                with contextlib.suppress(Exception):  # noqa: BLE001 - best-effort teardown
                    await js.delete_stream(name)
        finally:
            await client.drain()
            self._streams_ready = False

    # ---- 进程生命周期 --------------------------------------------------

    async def start(self) -> CollectorProcess:
        if self.process is not None:
            raise RuntimeError("harness already started")
        proc = start_sink_service(self.root, grpc_port=self.grpc_port, log_dir=self.root)
        self.process = proc
        await wait_tcp_port("127.0.0.1", self.grpc_port, timeout=30.0)
        await self.wait_grpc_ready()
        await self.wait_deliveries_running()
        return proc

    async def wait_deliveries_running(self, *, timeout: float = 45.0) -> None:
        """业务就绪屏障：全部 desired=START 的 Delivery 实际 RUNNING。

        gRPC 就绪只代表 Main 控制面可用；worker 订阅 NATS/JetStream 之前
        发出的 Core NATS 实时消息会永久丢失，因此发布输入前必须等到真实
        业务就绪（UC-01 的进程/业务就绪分离）。
        """

        async def _converged() -> bool:
            async with self.channel() as channel:
                response = await sink_service_pb2_grpc.SinkServiceStub(channel).ListDeliveries(
                    sink_service_pb2.ListDeliveriesRequest()
                )
            # 持久 STOP 意图在重启后合法保持 STOPPED（设计禁止重建 STOP
            # Delivery）；未收敛态为 UNKNOWN/PENDING/FAILED，必须继续等。
            return bool(response.deliveries) and all(
                item.actual_state in {"RUNNING", "STOPPED"} for item in response.deliveries
            )

        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                if await _converged():
                    return
            except Exception as exc:  # noqa: BLE001 - readiness polling
                last = exc
            await asyncio.sleep(0.5)
        raise RuntimeError(f"deliveries never reached RUNNING: {last!r}")

    async def stop(self) -> int:
        if self.process is None:
            return 0
        proc, self.process = self.process, None
        return await asyncio.to_thread(proc.terminate, timeout=30.0)

    def channel(self, *, caller: str = "admin") -> grpc.aio.Channel:
        key = f"{caller}_key"
        crt = f"{caller}_crt"
        credentials = grpc.ssl_channel_credentials(
            root_certificates=self.bundle["ca"].read_bytes(),
            private_key=self.bundle[key].read_bytes(),
            certificate_chain=self.bundle[crt].read_bytes(),
        )
        return grpc.aio.secure_channel(
            f"127.0.0.1:{self.grpc_port}",
            credentials,
            options=(("grpc.ssl_target_name_override", "localhost"),),
        )

    async def wait_grpc_ready(self, *, timeout: float = 45.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                async with self.channel() as ch:
                    info = await asyncio.wait_for(
                        common_pb2_grpc.CommonServiceStub(ch).GetServiceInfo(common_pb2.Empty()),
                        timeout=3,
                    )
                    status = await asyncio.wait_for(
                        common_pb2_grpc.CommonServiceStub(ch).GetServiceStatus(common_pb2.Empty()),
                        timeout=3,
                    )
                return {
                    "service_id": info.service_id,
                    "dependencies": [
                        {"name": d.name, "health": d.health} for d in status.dependencies
                    ],
                }
            except Exception as exc:  # noqa: BLE001 - readiness loop needs full capture
                last = exc
                await asyncio.sleep(0.5)
        raise RuntimeError(f"sink gRPC never became ready: {last!r}")

    # ---- 输入侧 -------------------------------------------------------

    def make_batch(
        self,
        mode: str,
        seq: int,
        points: int,
        *,
        device_id: str = DEVICE_ID,
        quality: str = "good",
        stale: bool = False,
    ) -> SampleBatch:
        now = datetime.now(UTC)
        sample_time = (
            now
            if not stale
            else datetime.fromtimestamp(now.timestamp() - self.staleness_ms / 1000.0 - 5, UTC)
        )
        samples = tuple(
            SampleItem(
                point_id=f"p{idx:03d}",
                sample_id=f"s-{mode}-{seq}-{idx}-{self.token[:4]}",
                value=float(seq + idx),
                quality=quality,  # type: ignore[arg-type]
                sample_time=sample_time,
                max_staleness_ms=self.staleness_ms if mode == "realtime" else None,
            )
            for idx in range(points)
        )
        return SampleBatch(
            batch_id=f"b-{mode}-{seq}-{self.token}",
            mode=mode,  # type: ignore[arg-type]
            wind_farm_id=WIND_FARM_ID,
            collector_id=COLLECTOR_ID,
            task_id="task-qa",
            device_id=device_id,
            publish_time=now,
            samples=samples,
        )

    async def publish_realtime(self, batches: list[SampleBatch], *, interval: float = 0.0) -> None:
        client = await nats.connect(servers=self.stack.nats, connect_timeout=10)
        try:
            for batch in batches:
                await client.publish(batch.subject(), batch.encode())
                await client.flush()
                if interval:
                    await asyncio.sleep(interval)
        finally:
            await client.drain()

    async def publish_reliable(self, batch: SampleBatch) -> Any:
        client = await nats.connect(servers=self.stack.nats, connect_timeout=10)
        try:
            return await client.jetstream().publish(batch.subject(), batch.encode(), timeout=10)
        finally:
            await client.drain()

    # ---- 边界观测 ------------------------------------------------------

    def redis_key(self, point_id: str, *, device_id: str = DEVICE_ID) -> str:
        return ":".join((self.namespace, WIND_FARM_ID, COLLECTOR_ID, device_id, point_id))

    async def wait_redis_value(
        self,
        key: str,
        *,
        timeout: float = 20.0,
        match: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        import json

        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", int(self.stack.redis.rpartition(":")[2])),
                    timeout=2,
                )
                writer.write(f"GET {key}\r\n".encode())
                await writer.drain()
                # RESP bulk string: $<len>\r\n<payload>\r\n
                header = await reader.readuntil(b"\r\n")
                length = int(header[1:].strip())
                if length < 0:
                    writer.close()
                    await writer.wait_closed()
                    await asyncio.sleep(0.2)
                    continue
                payload = await reader.readexactly(length + 2)
                writer.close()
                await writer.wait_closed()
                value = json.loads(payload[:-2].decode())
                if match is None or match(value):
                    return value
            except Exception as exc:  # noqa: BLE001
                last = exc
            await asyncio.sleep(0.2)
        raise TimeoutError(f"redis key {key} not observed: {last!r}")

    async def redis_ttl(self, key: str) -> int:
        host, _, port = self.stack.redis.rpartition(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(f"PTTL {key}\r\n".encode())
            await writer.drain()
            header = await reader.readuntil(b"\r\n")
            return int(header[1:].strip())
        finally:
            writer.close()
            await writer.wait_closed()

    async def redis_keys(self, pattern: str) -> list[str]:
        """解析 RESP 数组回复（``*n`` + 若干 bulk string）。"""
        host, _, port = self.stack.redis.rpartition(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        try:
            writer.write(f"KEYS {pattern}\r\n".encode())
            await writer.drain()
            array_header = await reader.readuntil(b"\r\n")
            count = int(array_header[1:].strip())
            if count <= 0:
                return []
            keys: list[str] = []
            for _ in range(count):
                bulk_header = await reader.readuntil(b"\r\n")
                length = int(bulk_header[1:].strip())
                payload = await reader.readexactly(length + 2)
                keys.append(payload[:-2].decode(errors="replace"))
            return keys
        finally:
            writer.close()
            await writer.wait_closed()

    async def stream_pending(self) -> dict[str, Any]:
        """JetStream 边界：流消息总数与本服务 durable 的未 ACK 积压。

        消息已投递但尚未 ACK 时计入 ``num_ack_pending`` 而非 ``num_pending``，
        未确认积压必须按两者之和判定（与生产 reliable_runtime 口径一致）。
        """
        client = await nats.connect(servers=self.stack.nats, connect_timeout=10)
        try:
            js = client.jetstream()
            info = await js.stream_info(self.history_stream)
            durable = await self._find_durable(js)
            pending = None
            if durable is not None:
                consumer = await js.consumer_info(self.history_stream, durable)
                pending = consumer.num_pending + consumer.num_ack_pending
            return {
                "stream_msgs": info.state.messages,
                "durable": durable,
                "consumer_pending": pending,
            }
        finally:
            await client.drain()

    async def consumer_state(self) -> dict[str, Any]:
        """durable 的未 ACK 积压与累计重投（JetStream 服务端真值）。"""
        client = await nats.connect(servers=self.stack.nats, connect_timeout=10)
        try:
            js = client.jetstream()
            durable = await self._find_durable(js)
            if durable is None:
                return {
                    "durable": None,
                    "num_pending": None,
                    "num_ack_pending": None,
                    "unacked": None,
                    "num_redelivered": None,
                }
            info = await js.consumer_info(self.history_stream, durable)
            return {
                "durable": durable,
                "num_pending": info.num_pending,
                "num_ack_pending": info.num_ack_pending,
                "unacked": info.num_pending + info.num_ack_pending,
                "num_redelivered": info.num_redelivered,
            }
        finally:
            await client.drain()

    async def dlq_count(self) -> int:
        """DLQ Stream 当前记录数（JetStream 服务端真值，不经被测服务）。"""
        client = await nats.connect(servers=self.stack.nats, connect_timeout=10)
        try:
            js = client.jetstream()
            info = await js.stream_info(self.dlq_stream)
            return int(info.state.messages)
        finally:
            await client.drain()

    async def dlq_latest(self) -> Any | None:
        """读取 DLQ Subject 上最新一条记录；空流返回 None。

        直接用 JetStream get_last_msg 做边界观测，避免跨连接复用
        AckPolicy.NONE durable 在 fetch 超时后的位置/排空不确定性。
        """
        from nats.js.errors import NotFoundError

        client = await nats.connect(servers=self.stack.nats, connect_timeout=10)
        try:
            js = client.jetstream()
            try:
                return await js.get_last_msg(self.dlq_stream, self.dlq_subject)
            except NotFoundError:
                return None
        finally:
            await client.drain()

    async def publish_raw_reliable(self, payload: bytes, *, device_id: str = DEVICE_ID) -> Any:
        """直接向可靠 subject 发布原始字节（用于畸形批/DLQ 注入）。"""
        client = await nats.connect(servers=self.stack.nats, connect_timeout=10)
        try:
            subject = f"wh.hist.batch.{WIND_FARM_ID}.{COLLECTOR_ID}.{device_id}"
            return await client.jetstream().publish(subject, payload, timeout=10)
        finally:
            await client.drain()

    async def events(self, *, page_size: int = 100) -> list[Any]:
        async with self.channel() as channel:
            response = await common_pb2_grpc.CommonServiceStub(channel).GetEvents(
                common_pb2.GetEventsRequest(page_size=page_size)
            )
        return list(response.events)

    async def _find_durable(self, js: Any) -> str | None:
        # nats-py 2.10+ 用 consumers_info 列举消费者；2.16 已无 consumer_names。
        infos = await js.consumers_info(self.history_stream)
        for info in infos:
            if info.name.startswith("WH_SINK_"):
                return info.name
        return infos[0].name if infos else None

    async def wait_kafka_messages(
        self, *, min_messages: int = 1, timeout: float = 45.0
    ) -> list[Any]:
        from aiokafka import AIOKafkaConsumer
        from aiokafka.errors import KafkaError

        deadline = time.monotonic() + timeout
        consumer: AIOKafkaConsumer | None = None
        # 容器 pause/unpause 后 KRaft 监听恢复有数秒窗口，bootstrap 首次
        # 失败时在总超时预算内重建消费者，而不是把环境抖动误判为链路失败。
        while time.monotonic() < deadline and consumer is None:
            candidate = AIOKafkaConsumer(
                self.kafka_topic,
                bootstrap_servers=self.stack.kafka,
                auto_offset_reset="earliest",
                group_id=f"qa-probe-{self.token}",
                enable_auto_commit=False,
            )
            try:
                await candidate.start()
                consumer = candidate
            except KafkaError:
                await candidate.stop()
                await asyncio.sleep(1.0)
        if consumer is None:
            return []
        try:
            records: list[Any] = []
            while time.monotonic() < deadline and len(records) < min_messages:
                result = await consumer.getmany(timeout_ms=2000, max_records=100)
                for messages in result.values():
                    records.extend(messages)
            return records
        finally:
            await consumer.stop()


@pytest.fixture
async def sink_harness(
    qa_stack: r5_stack.R5Stack, tmp_path: Path
) -> AsyncIterator[SinkSystemHarness]:
    token = secrets.token_hex(6)
    root = tmp_path / f"sink-root-{token}"
    bundle = write_mtls_bundle(root)
    harness = SinkSystemHarness(
        root=root,
        stack=qa_stack,
        grpc_port=free_port(),
        namespace=f"windhub-qa-{token}",
        kafka_topic=f"wh-history-qa-{token}",
        history_stream=f"WH_HISTORY_QA_{token[:8].upper()}",
        dlq_stream=f"WH_DLQ_QA_{token[:8].upper()}",
        dlq_subject=f"wh.qa.dlq.{token}",
        token=token,
        bundle=bundle,
    )
    try:
        yield harness
    finally:
        if harness.process is not None:
            with contextlib.suppress(Exception):
                await harness.stop()
        await harness.delete_streams()


def worker_children(main_pid: int) -> dict[int, str]:
    """Main 的直接子进程 PID -> /proc 启动时间（clock ticks）。

    spawn worker 按 ROLES 顺序（realtime、reliable）依次启动并等待就绪，
    因此按启动时间升序即可稳定映射角色；测试再用 gRPC worker.<role> 健康
    状态做交叉验证，不依赖进程名实现细节。
    """
    out = subprocess.run(
        ["pgrep", "-P", str(main_pid)], capture_output=True, text=True, check=False
    )
    children: dict[int, str] = {}
    for line in out.stdout.split():
        pid = int(line.strip())
        try:
            # spawn 还会派生 multiprocessing.resource_tracker 进程；它不承载
            # realtime/reliable 任何角色，必须从角色 PID 映射中排除。
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\x00", b" ").decode()
            if "resource_tracker" in cmdline:
                continue
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            continue
        # comm 可含空格且被括号包裹；starttime 是右括号后第 20 个字段。
        after = stat[stat.rindex(")") + 2 :].split()
        children[pid] = after[19]
    return dict(sorted(children.items(), key=lambda item: int(item[1])))


def worker_pid_set(main_pid: int) -> set[int]:
    """当前承载角色的 worker PID 集合（不含 resource_tracker）。

    故障注入期间角色可能被隔离重建，启动顺序映射不再可靠；集合差异
    （旧 PID 消失 / 新 PID 出现）才是进程边界可核实的证据。
    """
    return set(worker_children(main_pid))


def role_pids(main_pid: int) -> dict[str, int]:
    """返回 ``{"realtime": pid, "reliable": pid}``（按启动顺序映射）。

    仅适用于稳定的双 worker 拓扑：Supervisor 按 ROLES 顺序依次 spawn 并
    等待各自 BOOT 就绪，初始启动顺序确定。重建后的角色识别必须使用
    ``worker_pid_set`` 的集合差异，不能再依赖启动顺序。
    """
    pids = list(worker_children(main_pid))
    assert len(pids) == 2, f"expected two spawn workers, got {pids}"
    return {"realtime": pids[0], "reliable": pids[1]}
