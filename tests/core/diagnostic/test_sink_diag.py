"""Sink 下游只读诊断 sink.py 契约。

- Redis/Kafka/gRPC/TDengine/Modbus 五类的正常、不可用、超时、配置非法。
- 禁用 Sink 跳过；未知 Sink fail-closed（ValueError）。
- 只读：Redis 只 open/health/close；Kafka 只取元数据；TDengine 只发
  SERVER_VERSION()；诊断失败消息不得包含凭据明文。
- benchmark_sink 只对 PASS 的健康检查测速，探针中途失败即停止。
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import MappingProxyType, SimpleNamespace
from typing import Any

import pytest

from core.application.diagnostic import DiagnosticStatus
from core.domain.sink import Sink
from core.infrastructure.diagnostic import sink as sink_diag


def make_sink(
    sink_id: str = "s1",
    *,
    kind: str = "redis",
    enabled: bool = True,
    **connection: Any,
) -> Sink:
    return Sink(
        sink_id=sink_id,
        kind=kind,
        enabled=enabled,
        connection=MappingProxyType(connection),
    )


@pytest.fixture
def snapshot_with(monkeypatch: pytest.MonkeyPatch):
    def install(*sinks: Sink) -> dict[str, Sink]:
        table = {s.sink_id: s for s in sinks}
        monkeypatch.setattr(
            sink_diag,
            "YamlConfigAdapter",
            lambda path: SimpleNamespace(load=lambda: SimpleNamespace(sinks=table)),
        )
        return table

    return install


class FakeRedisSink:
    def __init__(
        self,
        *,
        healthy: bool = True,
        message: str | None = None,
        open_raises: Exception | None = None,
        close_raises: Exception | None = None,
    ) -> None:
        self.calls: list[str] = []
        self._healthy = healthy
        self._message = message
        self._open_raises = open_raises
        self._close_raises = close_raises

    async def open(self) -> None:
        self.calls.append("open")
        if self._open_raises:
            raise self._open_raises

    async def health(self):
        self.calls.append("health")
        return SimpleNamespace(healthy=self._healthy, message=self._message)

    async def close(self) -> None:
        self.calls.append("close")
        if self._close_raises:
            raise self._close_raises


# --------------------------------------------------------------------------
# 通用
# --------------------------------------------------------------------------


async def test_unknown_sink_raises(snapshot_with) -> None:
    snapshot_with(make_sink())
    with pytest.raises(ValueError, match="unknown sink"):
        await sink_diag.check_sink("ignored", "missing")


async def test_missing_config_directory_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    # 不把文件缺失伪装成 UNKNOWN：配置加载失败必须向上抛出（fail-closed）。
    def boom(path: str):
        raise FileNotFoundError(path)

    monkeypatch.setattr(
        sink_diag,
        "YamlConfigAdapter",
        lambda path: SimpleNamespace(load=lambda: (_ for _ in ()).throw(FileNotFoundError(path))),
    )
    with pytest.raises(FileNotFoundError):
        await sink_diag.check_sink("no-such-dir", "s1")


async def test_disabled_sink_is_skipped_without_connecting(snapshot_with) -> None:
    sink = make_sink(kind="redis", enabled=False, host="127.0.0.1", port=6379)
    table = snapshot_with(sink)
    spy = FakeRedisSink()

    import core.infrastructure.diagnostic.sink as module

    original = module.RedisSink
    module.RedisSink = lambda config: spy  # type: ignore[assignment]
    try:
        result = await sink_diag.check_sink("ignored", sink.sink_id)
    finally:
        module.RedisSink = original  # type: ignore[assignment]

    assert result.status is DiagnosticStatus.SKIPPED
    assert result.findings[0].code == "SINK_DISABLED"
    assert spy.calls == []
    assert table  # table installed


# --------------------------------------------------------------------------
# Redis
# --------------------------------------------------------------------------


async def test_redis_healthy_is_pass_and_closed(snapshot_with, monkeypatch) -> None:
    snapshot_with(make_sink(kind="redis", host="127.0.0.1", port=6379))
    fake = FakeRedisSink()
    monkeypatch.setattr(sink_diag, "RedisSink", lambda config: fake)

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.PASS
    assert result.findings[0].code == "SINK_HEALTH_OK"
    assert fake.calls == ["open", "health", "close"]


async def test_redis_unhealthy_is_fail(snapshot_with, monkeypatch) -> None:
    snapshot_with(make_sink(kind="redis"))
    monkeypatch.setattr(
        sink_diag, "RedisSink", lambda config: FakeRedisSink(healthy=False, message="PING wrong")
    )

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "SINK_HEALTH_FAILED"
    assert "PING wrong" in result.findings[0].message


async def test_redis_open_failure_is_fail_and_closed(snapshot_with, monkeypatch) -> None:
    snapshot_with(make_sink(kind="redis"))
    fake = FakeRedisSink(open_raises=ConnectionError("refused"))
    monkeypatch.setattr(sink_diag, "RedisSink", lambda config: fake)

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "SINK_CONNECT_FAILED"
    assert fake.calls == ["open", "close"]


async def test_redis_close_failure_is_swallowed(snapshot_with, monkeypatch) -> None:
    snapshot_with(make_sink(kind="redis"))
    monkeypatch.setattr(
        sink_diag,
        "RedisSink",
        lambda config: FakeRedisSink(close_raises=OSError("broken pipe")),
    )

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.PASS


# --------------------------------------------------------------------------
# Modbus / gRPC / Kafka
# --------------------------------------------------------------------------


async def test_modbus_sink_reports_server_context_required(snapshot_with) -> None:
    snapshot_with(make_sink(kind="modbus", host="192.0.2.10", port=502))

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.UNKNOWN
    assert result.findings[0].code == "SINK_SERVER_CONTEXT_REQUIRED"
    assert "192.0.2.10:502" in result.findings[0].message


class _FakeGrpcModule:
    class RpcError(Exception):
        pass

    class aio:  # noqa: N801 - mirrors grpc.aio namespace
        @staticmethod
        def insecure_channel(endpoint: str):
            return _FakeGrpcModule._Channel(endpoint)

    class _Channel:
        def __init__(self, endpoint: str) -> None:
            self.endpoint = endpoint
            self.closed = False

        async def channel_ready(self):
            if _FakeGrpcModule._mode == "fail":
                raise _FakeGrpcModule.RpcError("UNAVAILABLE")
            if _FakeGrpcModule._mode == "timeout":
                raise TimeoutError
            return True

        async def close(self) -> None:
            self.closed = True
            _FakeGrpcModule._last_closed = True

        def __aenter__(self):  # pragma: no cover - not used by production code
            raise AssertionError

        def __aexit__(self, *args):  # pragma: no cover
            raise AssertionError

    _mode = "ready"
    _last_closed = False


def _install_fake_grpc(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    _FakeGrpcModule._mode = mode
    _FakeGrpcModule._last_closed = False
    fake_module = types.ModuleType("grpc")
    fake_module.RpcError = _FakeGrpcModule.RpcError
    fake_module.aio = _FakeGrpcModule.aio
    monkeypatch.setitem(sys.modules, "grpc", fake_module)


async def test_grpc_ready_is_unknown_service_unverified_and_closed(
    snapshot_with, monkeypatch
) -> None:
    snapshot_with(make_sink(kind="grpc", endpoint="127.0.0.1:50073"))
    _install_fake_grpc(monkeypatch, "ready")

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.UNKNOWN
    assert result.findings[0].code == "GRPC_SERVICE_UNVERIFIED"
    assert _FakeGrpcModule._last_closed


@pytest.mark.parametrize("mode", ["fail", "timeout"])
async def test_grpc_unavailable_is_fail(snapshot_with, monkeypatch, mode: str) -> None:
    snapshot_with(make_sink(kind="grpc", endpoint="127.0.0.1:50073"))
    _install_fake_grpc(monkeypatch, mode)

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "GRPC_CHANNEL_UNAVAILABLE"


class FakeKafkaConsumer:
    """可编排生命周期的 AIOKafkaConsumer 替身。

    scenario 键：
    - start: ok | fail | slow | gate（gate 时等待 release_start）
    - topics: ok | fail | slow | missing
    - stop: ok | fail | hang
    start/stop 协程是否被取消分别记录在 start_cancelled/stop_cancelled。
    """

    instances: list[FakeKafkaConsumer] = []
    scenario: dict[str, str] = {}
    release_start: asyncio.Event | None = None

    def __init__(self, *args, **kwargs) -> None:
        self.calls: list[str] = []
        self.start_cancelled = False
        self.stop_cancelled = False
        FakeKafkaConsumer.instances.append(self)

    async def start(self) -> None:
        self.calls.append("start")
        behavior = FakeKafkaConsumer.scenario.get("start", "ok")
        if behavior == "fail":
            raise ConnectionError("broker down")
        if behavior == "slow":
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.start_cancelled = True
                raise
        elif behavior == "gate" and FakeKafkaConsumer.release_start is not None:
            await FakeKafkaConsumer.release_start.wait()

    async def topics(self):
        self.calls.append("topics")
        behavior = FakeKafkaConsumer.scenario.get("topics", "ok")
        if behavior == "fail":
            raise TimeoutError
        if behavior == "slow":
            await asyncio.sleep(30)
        return {"other"} if behavior == "missing" else {"wind", "other"}

    async def stop(self) -> None:
        # 同步即记账：即使 wait_for 随后取消 stop 任务，也能观察到释放动作被发起。
        self.calls.append("stop")
        behavior = FakeKafkaConsumer.scenario.get("stop", "ok")
        if behavior == "fail":
            raise RuntimeError("stop failed")
        if behavior == "hang":
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.stop_cancelled = True
                raise


def _install_fake_kafka(
    monkeypatch: pytest.MonkeyPatch,
    scenario: dict[str, str] | None = None,
    *,
    release_start: asyncio.Event | None = None,
) -> None:
    FakeKafkaConsumer.instances = []
    FakeKafkaConsumer.scenario = scenario or {}
    FakeKafkaConsumer.release_start = release_start
    aiokafka = types.ModuleType("aiokafka")
    aiokafka.AIOKafkaConsumer = FakeKafkaConsumer
    monkeypatch.setitem(sys.modules, "aiokafka", aiokafka)


async def test_kafka_topic_present_is_unknown_write_unverified(snapshot_with, monkeypatch) -> None:
    snapshot_with(make_sink(kind="kafka", bootstrap_servers="127.0.0.1:9092", topic="wind"))
    _install_fake_kafka(monkeypatch)

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.UNKNOWN
    assert result.findings[0].code == "KAFKA_WRITE_UNVERIFIED"
    consumer = FakeKafkaConsumer.instances[0]
    assert consumer.calls == ["start", "topics", "stop"]


async def test_kafka_topic_absent_is_fail_and_stopped(snapshot_with, monkeypatch) -> None:
    snapshot_with(make_sink(kind="kafka", bootstrap_servers="127.0.0.1:9092", topic="wind"))
    _install_fake_kafka(monkeypatch, {"topics": "missing"})

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "KAFKA_TOPIC_NOT_FOUND"
    assert FakeKafkaConsumer.instances[0].calls == ["start", "topics", "stop"]


@pytest.mark.parametrize(
    "scenario",
    [
        {"start": "fail"},
        {"topics": "fail"},
    ],
)
async def test_kafka_metadata_failures_are_fail_and_stopped(
    snapshot_with, monkeypatch, scenario
) -> None:
    snapshot_with(make_sink(kind="kafka", bootstrap_servers="x", topic="wind"))
    _install_fake_kafka(monkeypatch, scenario)

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "KAFKA_METADATA_FAILED"
    consumer = FakeKafkaConsumer.instances[0]
    # 989b413b 修复后：任何元数据阶段失败都必须完成 stop()，不残留 consumer。
    assert consumer.calls[0] == "start"
    assert consumer.calls[-1] == "stop"


async def test_kafka_start_timeout_still_stops_consumer(snapshot_with, monkeypatch) -> None:
    """start() 超时：wait_for 取消在途 start 后仍必须 stop()，总耗时有界。"""
    snapshot_with(make_sink(kind="kafka", bootstrap_servers="x", topic="wind"))
    _install_fake_kafka(monkeypatch, {"start": "slow"})

    loop = asyncio.get_running_loop()
    began = loop.time()
    result = await sink_diag.check_sink("ignored", "s1", timeout=0.1)
    elapsed = loop.time() - began

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "KAFKA_METADATA_FAILED"
    consumer = FakeKafkaConsumer.instances[0]
    assert consumer.start_cancelled is True
    assert consumer.calls == ["start", "stop"]
    assert elapsed < 3.0  # 不允许跟随 30s 假协程悬挂


async def test_kafka_topics_timeout_still_stops_consumer(snapshot_with, monkeypatch) -> None:
    snapshot_with(make_sink(kind="kafka", bootstrap_servers="x", topic="wind"))
    _install_fake_kafka(monkeypatch, {"topics": "slow"})

    result = await sink_diag.check_sink("ignored", "s1", timeout=0.1)

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "KAFKA_METADATA_FAILED"
    assert FakeKafkaConsumer.instances[0].calls == ["start", "topics", "stop"]


async def test_kafka_stop_failure_is_bounded_and_reported(snapshot_with, monkeypatch) -> None:
    """stop() 自身抛错：不掩盖失败状态、不悬挂、不外泄异常。"""
    snapshot_with(make_sink(kind="kafka", bootstrap_servers="127.0.0.1:9092", topic="wind"))
    _install_fake_kafka(monkeypatch, {"stop": "fail"})

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "KAFKA_METADATA_FAILED"
    assert "RuntimeError" in result.findings[0].message
    assert FakeKafkaConsumer.instances[0].calls[-1] == "stop"


async def test_kafka_hanging_stop_is_cut_off_by_timeout(snapshot_with, monkeypatch) -> None:
    """stop() 挂死：wait_for(stop, timeout) 必须斩断等待，诊断总耗时有界。"""
    snapshot_with(make_sink(kind="kafka", bootstrap_servers="127.0.0.1:9092", topic="wind"))
    _install_fake_kafka(monkeypatch, {"stop": "hang"})

    loop = asyncio.get_running_loop()
    began = loop.time()
    result = await sink_diag.check_sink("ignored", "s1", timeout=0.1)
    elapsed = loop.time() - began

    assert result.status is DiagnosticStatus.FAIL
    consumer = FakeKafkaConsumer.instances[0]
    assert consumer.stop_cancelled is True
    assert consumer.calls == ["start", "topics", "stop"]
    assert elapsed < 3.0


@pytest.mark.parametrize("hang_phase", ["start", "topics"])
async def test_kafka_external_cancellation_releases_consumer(
    snapshot_with, monkeypatch, hang_phase: str
) -> None:
    """check_sink 被外部取消：取消必须传播，且 stop() 被发起/等待，不留悬挂 consumer。

    分别在 start() 与 topics() 两个等待点取消。
    """
    snapshot_with(make_sink(kind="kafka", bootstrap_servers="x", topic="wind"))
    entered = asyncio.Event()
    gate = asyncio.Event()

    class _GateConsumer(FakeKafkaConsumer):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.cancelled_phase: str | None = None

        async def start(self) -> None:
            self.calls.append("start")
            if hang_phase == "start":
                entered.set()
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    self.cancelled_phase = "start"
                    raise

        async def topics(self):
            self.calls.append("topics")
            if hang_phase == "topics":
                entered.set()
                try:
                    await gate.wait()
                except asyncio.CancelledError:
                    self.cancelled_phase = "topics"
                    raise
            return {"wind"}

    aiokafka = types.ModuleType("aiokafka")
    aiokafka.AIOKafkaConsumer = _GateConsumer
    # 清空类级场景：_GateConsumer 继承父类 stop()，前序 stop fail/hang
    # 用例残留的 scenario 会让释放路径抛错或挂死，替换正在传播的 CancelledError。
    FakeKafkaConsumer.instances = []
    FakeKafkaConsumer.scenario = {}
    FakeKafkaConsumer.release_start = None
    monkeypatch.setitem(sys.modules, "aiokafka", aiokafka)

    task = asyncio.create_task(sink_diag.check_sink("ignored", "s1", timeout=5))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    consumer = FakeKafkaConsumer.instances[0]
    assert consumer.calls[0] == "start"
    assert consumer.calls[-1] == "stop"
    # wait_for 必须真正取消在途协程，而不是放任其悬挂。
    assert consumer.cancelled_phase == hang_phase


async def test_kafka_diagnostic_never_writes(snapshot_with, monkeypatch) -> None:
    """只读契约：整个诊断只触碰 start/topics/stop，不存在任何发送类方法调用。"""
    snapshot_with(make_sink(kind="kafka", bootstrap_servers="127.0.0.1:9092", topic="wind"))
    _install_fake_kafka(monkeypatch)

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.UNKNOWN
    consumer = FakeKafkaConsumer.instances[0]
    assert set(consumer.calls) <= {"start", "topics", "stop"}
    assert not any(name in consumer.calls for name in ("send", "send_and_wait", "produce"))


# --------------------------------------------------------------------------
# TDengine：受控 HTTP，不产生写入，凭据不出现在失败消息中
# --------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        pass


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, behavior: Any) -> tuple[list[Any], Any]:
    requests: list[Any] = []

    def fake_urlopen(request: Any, timeout: float) -> Any:
        requests.append((request, timeout))
        if isinstance(behavior, Exception):
            raise behavior
        return FakeResponse(behavior)

    monkeypatch.setattr(sink_diag.urllib.request, "urlopen", fake_urlopen)
    return requests, behavior


@pytest.mark.parametrize(
    ("payload", "status", "code"),
    [
        ({"code": 0}, DiagnosticStatus.PASS, "TDENGINE_QUERY_OK"),
        ({"code": 0x2602, "desc": "invalid"}, DiagnosticStatus.FAIL, "TDENGINE_QUERY_REJECTED"),
    ],
)
async def test_tdengine_readonly_query_outcomes(
    snapshot_with, monkeypatch, payload, status, code
) -> None:
    snapshot_with(
        make_sink(
            kind="tdengine",
            host="127.0.0.1",
            port=6041,
            database="wind_hub",
            username="diag",
            password="s3cr3t-token",
        )
    )
    requests, _ = _patch_urlopen(monkeypatch, payload)

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is status
    assert result.findings[0].code == code
    request, timeout = requests[0]
    body = request.data.decode()
    assert body == "SELECT SERVER_VERSION()"
    assert "INSERT" not in body.upper() and "CREATE" not in body.upper()
    assert request.get_method() == "POST"
    assert timeout > 0


async def test_tdengine_accepts_secret_provider_password(monkeypatch) -> None:
    # pydantic SecretStr 形态的密码走 get_secret_value() 分支；
    # 该对象无法通过 Sink 冻结校验，直接在只读函数层验证。
    connection = {
        "host": "127.0.0.1",
        "port": 6041,
        "database": "wind_hub",
        "username": "diag",
        "password": SimpleNamespace(get_secret_value=lambda: "s3cr3t-token"),
    }
    _patch_urlopen(monkeypatch, {"code": 0})

    result = await sink_diag._tdengine_readonly("t", connection, 2.0)

    assert result.status is DiagnosticStatus.PASS


async def test_tdengine_http_error_is_fail_without_credential_leak(
    snapshot_with, monkeypatch
) -> None:
    snapshot_with(make_sink(kind="tdengine", host="h", password="s3cr3t-token", username="diag"))
    _patch_urlopen(monkeypatch, OSError("connection refused"))

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "TDENGINE_QUERY_FAILED"
    assert "s3cr3t-token" not in result.findings[0].message


async def test_tdengine_missing_password_is_fail(snapshot_with) -> None:
    snapshot_with(make_sink(kind="tdengine", host="h", username="root"))

    result = await sink_diag.check_sink("ignored", "s1")

    assert result.findings[0].code == "SINK_CREDENTIALS_MISSING"
    assert result.status is DiagnosticStatus.FAIL


async def test_tdengine_non_mapping_connection_is_fail(snapshot_with) -> None:
    # Sink 领域对象的 connection 恒为 Mapping；非 Mapping 只能来自内部误用，
    # 直接调用只读函数验证 fail-closed。
    result = await sink_diag._tdengine_readonly("t", "not-a-mapping", 1.0)
    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "SINK_CONFIG_INVALID"


# --------------------------------------------------------------------------
# benchmark_sink
# --------------------------------------------------------------------------


async def test_benchmark_sink_measures_pass_health_only(snapshot_with, monkeypatch) -> None:
    snapshot_with(make_sink(kind="redis"))
    monkeypatch.setattr(sink_diag, "RedisSink", lambda config: FakeRedisSink())

    result = await sink_diag.benchmark_sink("ignored", "s1", samples=3, interval=0)

    assert result.status is DiagnosticStatus.PASS
    assert result.category == "sink"
    assert result.measurements["measurement"] == "health_probe_latency_only"
    assert result.measurements["requests"] == 3


async def test_benchmark_sink_unavailable_returns_failure_directly(
    snapshot_with, monkeypatch
) -> None:
    snapshot_with(make_sink(kind="redis"))
    monkeypatch.setattr(
        sink_diag, "RedisSink", lambda config: FakeRedisSink(open_raises=OSError("down"))
    )

    result = await sink_diag.benchmark_sink("ignored", "s1")

    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "SINK_CONNECT_FAILED"


async def test_benchmark_sink_records_every_failed_probe(snapshot_with, monkeypatch) -> None:
    class DegradingSink(FakeRedisSink):
        def __init__(self) -> None:
            super().__init__()
            self.health_calls = 0

        async def health(self):
            self.calls.append("health")
            self.health_calls += 1
            if self.health_calls >= 2:
                self._healthy = False
            return await super().health()

    snapshot_with(make_sink(kind="redis"))
    degrading = DegradingSink()
    monkeypatch.setattr(sink_diag, "RedisSink", lambda config: degrading)

    result = await sink_diag.benchmark_sink("ignored", "s1", samples=5, interval=0)

    # 预检 PASS、首个探针即失败：benchmark 不重试、不短路，
    # 5 个样本全部计失败，汇总 FAIL（finding code 固定 BENCHMARK_COMPLETE）。
    assert result.status is DiagnosticStatus.FAIL
    assert result.findings[0].code == "BENCHMARK_COMPLETE"
    assert result.measurements["successes"] == 0
    assert result.measurements["failures"] == 5


async def test_check_sink_timeout_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # open 超过诊断超时 → asyncio.timeout 转换为 FAIL 结果，不悬挂。
    class HangingSink(FakeRedisSink):
        async def open(self) -> None:
            self.calls.append("open")
            await asyncio.sleep(1.0)

    monkeypatch.setattr(
        sink_diag,
        "YamlConfigAdapter",
        lambda path: SimpleNamespace(
            load=lambda: SimpleNamespace(sinks={"s1": make_sink(kind="redis")})
        ),
    )
    monkeypatch.setattr(sink_diag, "RedisSink", lambda config: HangingSink())

    result = await sink_diag.check_sink("ignored", "s1", timeout=0.05)

    assert result.status is DiagnosticStatus.FAIL


async def test_unknown_sink_kind_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sink 领域对象禁止非法 kind；该分支是防御深度，用最小鸭子类型直接验证。
    fake = SimpleNamespace(
        sink_id="weird",
        kind="file",
        enabled=True,
        connection=MappingProxyType({}),
    )
    monkeypatch.setattr(
        sink_diag,
        "YamlConfigAdapter",
        lambda path: SimpleNamespace(load=lambda: SimpleNamespace(sinks={"weird": fake})),
    )

    result = await sink_diag.check_sink("ignored", "weird")

    assert result.status is DiagnosticStatus.UNKNOWN
    assert result.findings[0].code == "SINK_PROTOCOL_NOT_VERIFIED"
