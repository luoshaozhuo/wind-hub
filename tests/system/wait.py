"""System / Recovery 测试的统一等待原语——全部是带超时的轮询，禁止硬 sleep。

每个 ``wait_*`` 在条件满足时返回观测到的数据，超时抛 :class:`WaitTimeout`
并附最近一次观测快照，保证失败报告可读（而不是一句 ``assert False``）。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


class WaitTimeout(TimeoutError):
    """等待条件在超时窗口内未满足（消息含最近观测快照）。"""


async def wait_until(
    probe: Callable[[], Awaitable[T | None]],
    *,
    timeout: float = 15.0,
    interval: float = 0.1,
    description: str = "condition",
) -> T:
    """异步轮询直到 ``probe()`` 返回非 None 值。

    Args:
        probe: 探测函数；返回 None 表示条件未满足，其余返回值即观测结果。
        timeout: 总超时（秒）。
        interval: 轮询间隔（秒）。
        description: 条件描述（用于超时消息）。

    Returns:
        probe 首次返回的非 None 值。

    Raises:
        WaitTimeout: 超时未满足。
    """
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            result = await probe()
        except Exception as exc:  # 探测期间依赖尚未就绪是常态，记录后继续轮询
            last_error = exc
            result = None
        if result is not None:
            return result
        await asyncio.sleep(interval)
    detail = f"; last probe error: {last_error!r}" if last_error else ""
    raise WaitTimeout(f"timeout after {timeout}s waiting for {description}{detail}")


async def wait_grpc_ready(target: str, *, timeout: float = 30.0) -> str:
    """等待 Collector gRPC 控制面可连接且 Runtime 就绪。

    通过真实 ``GetRuntimeStatus`` RPC 判定（而非仅 TCP 可连——gRPC 监听
    先于 Runtime 启动完成，仅探测端口会拿到未就绪的 Collector）。
    """

    async def _probe() -> str | None:
        import grpc
        from google.protobuf import empty_pb2

        from wind_hub_core.rpc.collector import (
            GET_RUNTIME_STATUS,
            RUNTIME_SERVICE,
            rpc_path,
        )

        channel = grpc.aio.insecure_channel(target)
        try:
            call = channel.unary_unary(rpc_path(RUNTIME_SERVICE, GET_RUNTIME_STATUS))
            response = await call(
                empty_pb2.Empty(),
                timeout=2.0,
            )
            # Struct 响应能解码即认为控制面就绪。
            return response.decode("utf-8", errors="replace")
        finally:
            await channel.close()

    return await wait_until(
        _probe,
        timeout=timeout,
        interval=0.2,
        description=f"grpc ready at {target}",
    )


async def wait_process_exit(
    proc_wait: Callable[[], int | None],
    *,
    timeout: float = 20.0,
    description: str = "process exit",
) -> int:
    """等待 subprocess 退出并返回退出码（``proc_wait`` 通常是 ``Popen.poll``）。"""

    async def _probe() -> int | None:
        return proc_wait()

    return await wait_until(_probe, timeout=timeout, interval=0.1, description=description)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 File Sink 的 JSONL 输出（跳过空行；含 gzip 分片不解压）。"""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


async def wait_file_rows(
    path: Path,
    *,
    min_rows: int = 1,
    timeout: float = 20.0,
    match: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """等待 JSONL 文件出现至少 ``min_rows`` 行（可选按 ``match`` 过滤）。"""

    async def _probe() -> list[dict[str, Any]] | None:
        rows = read_jsonl(path)
        if match is not None:
            rows = [row for row in rows if match(row)]
        return rows if len(rows) >= min_rows else None

    return await wait_until(
        _probe,
        timeout=timeout,
        description=f"{min_rows} rows in {path}",
    )


async def wait_kafka_messages(
    bootstrap_servers: str,
    topic: str,
    *,
    min_messages: int = 1,
    timeout: float = 30.0,
    match: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """用独立 consumer（每次随机 group、from earliest）等待 Kafka 消息。

    消息体按 UTF-8 JSON 解码；``match`` 过滤解码后的 dict。consumer 与
    被测 Collector 完全独立——这是系统边界验证，不是内部状态窥探。
    """

    async def _collect() -> list[dict[str, Any]]:
        import uuid

        from aiokafka import AIOKafkaConsumer

        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=bootstrap_servers,
            group_id=f"wind-hub-test-{uuid.uuid4().hex[:12]}",
            auto_offset_reset="earliest",
            enable_auto_commit=False,
        )
        await consumer.start()
        try:
            deadline = time.monotonic() + timeout
            messages: list[dict[str, Any]] = []
            while time.monotonic() < deadline:
                batch = await consumer.getmany(timeout_ms=500)
                for records in batch.values():
                    for record in records:
                        data = json.loads(record.value.decode("utf-8"))
                        if match is None or match(data):
                            messages.append(data)
                if len(messages) >= min_messages:
                    return messages
            return messages
        finally:
            await consumer.stop()

    messages = await _collect()
    if len(messages) < min_messages:
        raise WaitTimeout(
            f"timeout after {timeout}s waiting for {min_messages} kafka messages "
            f"on {topic} (got {len(messages)})"
        )
    return messages


async def wait_postgres_rows(
    dsn: str,
    table: str,
    *,
    min_rows: int = 1,
    timeout: float = 30.0,
    where: str | None = None,
) -> list[dict[str, Any]]:
    """用独立 SQL 连接等待 PostgreSQL 表出现至少 ``min_rows`` 行。"""

    async def _probe() -> list[dict[str, Any]] | None:
        import asyncpg

        conn = await asyncpg.connect(dsn)
        try:
            sql = f"SELECT * FROM {table}"
            if where:
                sql += f" WHERE {where}"
            rows = await conn.fetch(sql)
            result = [dict(row) for row in rows]
            return result if len(result) >= min_rows else None
        finally:
            await conn.close()

    return await wait_until(
        _probe,
        timeout=timeout,
        interval=0.5,
        description=f"{min_rows} rows in postgres table {table}",
    )
