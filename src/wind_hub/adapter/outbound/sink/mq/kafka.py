"""Kafka 输出 sink —— 用 aiokafka 把点值批量投递到消息主题。

实现 :class:`~wind_hub.domain.port.outbound.SinkPort` 的真实 Kafka 走向：把一批
:class:`~wind_hub.domain.model.point.PointValue` 序列化成 UTF-8 的 JSON 消息，
经 ``aiokafka.AIOKafkaProducer`` 投递到 ``topic``；``key_field`` 指定后以点值该
字段（``device_id`` / ``point_id`` / ``source``）作为消息 key 实现分区亲和，
否则 key 置空（轮询分区）。

参数在**构造时**校验（缺 ``bootstrap_servers`` / ``topic`` 抛
:class:`~wind_hub.domain.model.errors.ConfigError`）；运行时状态由
``asyncio.Lock`` 保护，仅在调度器所属同一事件循环内被调用；投递失败抛
:class:`~wind_hub.domain.model.errors.SinkError`，连续失败达到阈值后
``health()`` 报告 unhealthy（决策 6/8，复用 FileSink 模式）。

注意 ``send()`` 入队即返回，broker ack 异步完成：每次 ``send`` 返回的
future 被暂存，在后续 ``write``（收割已决项）与 ``flush``（gather 全部）
时核对——broker 端失败据此累计进失败计数并写入
``sink_write_failures_total`` 指标（决策 3），否则 broker 故障不可见。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

# aiokafka 0.14 未随包发布 py.typed / 类型桩，mypy 会把该模块标记为
# `import-untyped`；此处按可选额外依赖的标准做法显式抑制，避免泄漏 aiokafka
# 类型到对外接口（``self._producer`` 内部按 ``Any`` 处理，公开签名只出现
# PointValue / HealthStatus / SinkError）。
from aiokafka import AIOKafkaProducer  # type: ignore[import-untyped]

from wind_hub.config.schema import SinkConfig
from wind_hub.domain.model.errors import ConfigError, SinkError
from wind_hub.domain.model.point import PointValue
from wind_hub.domain.port.outbound import HealthStatus, SinkPort
from wind_hub.infra import metrics

logger = logging.getLogger(__name__)

# 连续投递失败达到该次数后，sink 标记为 unhealthy（决策 8，复用 FileSink 阈值）。
_MAX_CONSECUTIVE_FAILURES = 5

# ``key_field`` 允许的点值字段（均为字符串标识，适合做分区 key）。
_KEY_FIELDS = ("device_id", "point_id", "source")


class KafkaSink(SinkPort):
    """把点值批量投递到 Kafka 主题的输出 sink。

    参数（``SinkConfig.params``）：

    - ``bootstrap_servers``（必填）：逗号分隔或单机 broker 地址（如 ``localhost:9092``）。
    - ``topic``（必填）：目标主题名。
    - ``key_field``：可选，指定作为消息 key 的点值字段，默认无 key。
    - ``compression_type``：可选，aiokafka 压缩类型（``gzip`` / ``snappy`` / ``lz4``
      / ``zstd``），默认不压缩。
    - ``acks``：生产者确认级别（``all`` / ``0`` / ``1``），默认 ``all``。
    - ``retries``：发送失败重试次数，默认 ``3``。
    - ``batch_size``：生产者批大小（字节），默认 ``16384``。
    - ``linger_ms``：批收集等待时长（毫秒），默认 ``0``。
    """

    def __init__(self, config: SinkConfig) -> None:
        params = config.params

        self._bootstrap_servers = self._require_str(params, "bootstrap_servers")
        self._topic = self._require_str(params, "topic")
        self._key_field = self._validate_key_field(params.get("key_field"))
        self._compression_type = self._optional_str(params, "compression_type")
        self._acks = self._str_or_int(params.get("acks", "all"), "acks")
        self._retries = self._positive_int(params.get("retries", 3), "retries")
        self._batch_size = self._positive_int(params.get("batch_size", 16384), "batch_size")
        self._linger_ms = self._nonnegative_int(params.get("linger_ms", 0), "linger_ms")

        # 运行时状态 —— 由 `asyncio.Lock` 保护；producer 在 open 后创建。
        self._name = config.name
        self._producer: Any = None
        # ``send()`` 返回的 broker-ack future 暂存于此：入队即返回的 send 不
        # 暴露 broker 端失败，需在 write（收割已决项）/ flush（gather 全部）
        # 时核对并累计失败计数（决策 3）。
        self._pending_futures: list[asyncio.Future[Any]] = []
        self._lock = asyncio.Lock()
        self._healthy = True
        self._error_message: str | None = None
        self._consecutive_failures = 0

    # -- 参数校验 ---------------------------------------------------------

    @staticmethod
    def _require_str(params: dict[str, Any], field: str) -> str:
        value = params.get(field)
        if not isinstance(value, str) or not value:
            raise ConfigError(f"KafkaSink '{field}' is required and must be a non-empty string")
        return value

    @staticmethod
    def _optional_str(params: dict[str, Any], field: str) -> str | None:
        value = params.get(field)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ConfigError(f"KafkaSink '{field}' must be a non-empty string, got {value!r}")
        return value

    @staticmethod
    def _str_or_int(value: object, field: str) -> str | int:
        if not isinstance(value, str | int) or isinstance(value, bool):
            raise ConfigError(f"KafkaSink '{field}' must be a string or integer, got {value!r}")
        return value

    @staticmethod
    def _validate_key_field(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or value not in _KEY_FIELDS:
            raise ConfigError(f"KafkaSink 'key_field' must be one of {_KEY_FIELDS}, got {value!r}")
        return value

    @staticmethod
    def _positive_int(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"KafkaSink '{field}' must be a positive integer, got {value!r}")
        return value

    @staticmethod
    def _nonnegative_int(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError(f"KafkaSink '{field}' must be a non-negative integer, got {value!r}")
        return value

    # -- SinkPort 契约 ----------------------------------------------------

    async def open(self) -> None:
        """创建并启动生产者；幂等，已启动时无操作。

        Raises:
            SinkError: broker 不可达（``start`` 失败）时抛出。
        """
        async with self._lock:
            if self._producer is not None:
                return
            producer = AIOKafkaProducer(
                bootstrap_servers=self._bootstrap_servers,
                acks=self._acks,
                retries=self._retries,
                batch_size=self._batch_size,
                linger_ms=self._linger_ms,
                compression_type=self._compression_type,
            )
            try:
                await producer.start()
            except Exception as exc:
                self._record_failure(f"open failed: {exc}")
                raise SinkError(f"KafkaSink open failed: {exc}") from exc
            self._producer = producer
            self._mark_healthy()

    async def close(self) -> None:
        """冲刷待投递消息并停止生产者；幂等，已关闭时无操作。

        Raises:
            SinkError: 冲刷或停止失败时抛出（生产者仍被释放）。
        """
        async with self._lock:
            if self._producer is None:
                return
            producer = self._producer
            self._producer = None
            # 取回已决 future 的异常引用再丢弃，避免解释器在 GC 时打印
            # "Future exception was never retrieved" 告警。
            for future in self._pending_futures:
                if future.done() and not future.cancelled():
                    future.exception()
            self._pending_futures.clear()
        try:
            await producer.flush()
        finally:
            await producer.stop()

    async def write(self, batch: list[PointValue]) -> None:
        """把整批点值序列化后逐条投递到主题。

        ``send()`` 入队即返回，broker ack 异步完成；返回的 future 被暂存，
        本次调用末尾收割已决项（剩余的在 ``flush`` 时核对）——broker 端
        失败（ack 失败）据此累计进失败计数（决策 3）。

        Raises:
            SinkError: 投递失败，或 ``open`` 尚未调用。
        """
        if not batch:
            return
        async with self._lock:
            if self._producer is None:
                raise SinkError("KafkaSink.write() called before open()")
            try:
                for pv in batch:
                    future = await self._producer.send(
                        self._topic,
                        value=self._serialize(pv),
                        key=self._message_key(pv),
                    )
                    self._pending_futures.append(future)
            except Exception as exc:
                metrics.sink_write_failures_total.labels(sink_name=self._name).inc()
                self._record_failure(f"write failed: {exc}")
                raise SinkError(f"KafkaSink write failed: {exc}") from exc
            if self._reap_done_futures() == 0:
                metrics.sink_writes_total.labels(sink_name=self._name).inc()
                metrics.sink_points_written_total.labels(sink_name=self._name).inc(len(batch))
                self._mark_healthy()

    async def flush(self) -> None:
        """冲刷生产者缓冲，并核对全部在途 send future 的 broker ack（决策 3）。

        broker 端失败（ack 失败）累计进失败计数并记入
        ``sink_write_failures_total``，达到阈值后 ``health()`` 报告
        unhealthy——否则 ``send()`` 入队即返回会让 broker 故障完全不可见。

        Raises:
            SinkError: ``producer.flush`` 本身失败时抛出。
        """
        async with self._lock:
            if self._producer is None:
                return
            producer = self._producer
            pending = list(self._pending_futures)
            self._pending_futures.clear()
        try:
            await producer.flush()
        except Exception as exc:
            self._record_failure(f"flush failed: {exc}")
            raise SinkError(f"KafkaSink flush failed: {exc}") from exc
        # 与 write 的收割路径一致：每条被 broker 拒收的消息计一次失败。
        failures = 0
        if pending:
            results = await asyncio.gather(*pending, return_exceptions=True)
            for res in results:
                if isinstance(res, BaseException):
                    failures += 1
                    self._record_failure(f"broker ack failed: {res}")
        if failures:
            metrics.sink_write_failures_total.labels(sink_name=self._name).inc(failures)
            logger.warning("KafkaSink '%s': %d message(s) rejected by broker", self._name, failures)
        else:
            self._mark_healthy()

    def health(self) -> HealthStatus:
        """返回缓存的健康状态；连续投递失败达到阈值后报告 unhealthy。"""
        return HealthStatus(healthy=self._healthy, message=self._error_message)

    # -- 内部：序列化 -----------------------------------------------------

    def _serialize(self, pv: PointValue) -> bytes:
        """把一个点值序列化为 UTF-8 JSON 消息体（决策 4）。"""
        return json.dumps(
            {
                "device_id": pv.device_id,
                "point_id": pv.point_id,
                "value": pv.value,
                "quality": pv.quality.value,
                "timestamp": pv.timestamp.isoformat(),
                "source": pv.source,
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def _message_key(self, pv: PointValue) -> bytes | None:
        """按 ``key_field`` 取点值字段作为消息 key；未配置或字段为空时返回 ``None``。"""
        if self._key_field is None:
            return None
        value = getattr(pv, self._key_field)
        if value is None:
            return None
        return str(value).encode("utf-8")

    # -- 内部：broker-ack future 核对（决策 3） ----------------------------

    def _reap_done_futures(self) -> int:
        """收割已完成的 send future：broker ack 失败计入失败计数与指标。

        返回本次收割到的失败数；调用方须已持有 ``self._lock``。未决的
        future 保留在 ``_pending_futures`` 中，留给后续 write/flush 核对。
        """
        remaining: list[asyncio.Future[Any]] = []
        failures = 0
        for future in self._pending_futures:
            if not future.done():
                remaining.append(future)
                continue
            if future.cancelled():
                failures += 1
                self._record_failure("broker send cancelled")
                continue
            exc = future.exception()
            if exc is not None:
                failures += 1
                self._record_failure(f"broker ack failed: {exc}")
        self._pending_futures = remaining
        if failures:
            metrics.sink_write_failures_total.labels(sink_name=self._name).inc(failures)
        return failures

    # -- 内部：健康跟踪 ---------------------------------------------------

    def _mark_healthy(self) -> None:
        """成功投递/冲刷后复位失败计数、恢复健康。"""
        self._consecutive_failures = 0
        self._healthy = True
        self._error_message = None

    def _record_failure(self, message: str) -> None:
        """累计失败次数，达到阈值后标记 unhealthy（决策 8）。"""
        self._consecutive_failures += 1
        self._error_message = message
        if self._consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            self._healthy = False
