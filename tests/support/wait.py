"""System / Recovery 测试的统一等待原语——全部是带超时的轮询，禁止硬 sleep。

每个 ``wait_*`` 在条件满足时返回观测到的数据，超时抛 :class:`WaitTimeoutError`
并附最近一次观测快照，保证失败报告可读（而不是一句 ``assert False``）。
"""

from __future__ import annotations

import asyncio
import csv
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")


class WaitTimeoutError(TimeoutError):
    """等待条件在超时窗口内未满足（消息含最近观测快照）。"""


async def wait_until(
    probe: Callable[[], Awaitable[T | None] | T | None],
    *,
    timeout: float = 15.0,
    interval: float = 0.1,
    description: str = "condition",
) -> T:
    """轮询直到 ``probe()`` 返回非 None 值（同步/异步 probe 均可）。

    Args:
        probe: 探测函数；返回 None 表示条件未满足，其余返回值即观测结果；
            返回 awaitable 时先 await 再判定。
        timeout: 总超时（秒）。
        interval: 轮询间隔（秒）。
        description: 条件描述（用于超时消息）。

    Returns:
        probe 首次返回的非 None 值。

    Raises:
        WaitTimeoutError: 超时未满足。
    """
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            result = probe()
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # 探测期间依赖尚未就绪是常态，记录后继续轮询
            last_error = exc
            result = None
        if result is not None:
            return result
        await asyncio.sleep(interval)
    detail = f"; last probe error: {last_error!r}" if last_error else ""
    raise WaitTimeoutError(f"timeout after {timeout}s waiting for {description}{detail}")


async def wait_grpc_ready(target: str, *, timeout: float = 30.0) -> str:
    """等待 Collector gRPC 控制面可连接且 Runtime 就绪。

    通过真实 ``GetRuntimeStatus`` RPC 判定（而非仅 TCP 可连——gRPC 监听
    先于 Runtime 启动完成，仅探测端口会拿到未就绪的 Collector）。
    """

    async def _probe() -> str | None:
        import grpc
        from google.protobuf import empty_pb2, json_format

        from wind_hub_core.rpc import collector_pb2_grpc as pb_grpc

        channel = grpc.aio.insecure_channel(target)
        try:
            stub = pb_grpc.CollectorRuntimeServiceStub(channel)
            response = await stub.GetRuntimeStatus(empty_pb2.Empty(), timeout=2.0)
            return json_format.MessageToJson(response)
        finally:
            await channel.close()

    return await wait_until(
        _probe,
        timeout=timeout,
        interval=0.2,
        description=f"grpc ready at {target}",
    )


async def wait_commander_ready(target: str, *, timeout: float = 30.0) -> str:
    """等待 Commander gRPC 控制面可连接（真实 ``GetStatus`` RPC 判定）。"""

    async def _probe() -> str | None:
        import grpc
        from google.protobuf import empty_pb2, json_format

        from wind_hub_core.rpc import commander_pb2_grpc as pb_grpc

        channel = grpc.aio.insecure_channel(target)
        try:
            stub = pb_grpc.CommanderServiceStub(channel)
            response = await stub.GetStatus(empty_pb2.Empty(), timeout=2.0)
            return json_format.MessageToJson(response)
        finally:
            await channel.close()

    return await wait_until(
        _probe,
        timeout=timeout,
        interval=0.2,
        description=f"commander grpc ready at {target}",
    )


async def wait_http_ready(target: str, *, timeout: float = 30.0) -> None:
    """等待 Server REST API 可应答（真实 ``/api/v1/overview`` GET 判定）。

    就绪口径是「HTTP 服务返回非 5xx」——overview 在 Worker 未全部就绪时
    可能返回 503，但进程尚处于启动中；仅 2xx/4xx 视为服务已接管端口。
    """

    async def _probe() -> bool | None:
        import httpx

        async with httpx.AsyncClient(base_url=f"http://{target}", timeout=2.0) as http:
            response = await http.get("/api/v1/overview")
            return True if response.status_code < 500 else None

    await wait_until(
        _probe,
        timeout=timeout,
        interval=0.2,
        description=f"http ready at {target}",
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


def read_csv(path: Path) -> list[dict[str, Any]]:
    """读取 FileSink CSV 长表；将 JSON 数值与空值恢复为便于断言的标量。"""
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for parsed_row in csv.DictReader(handle):
            row: dict[str, Any] = dict(parsed_row)
            value = row["value"]
            if not value:
                row["value"] = None
            elif value == "True":
                row["value"] = True
            elif value == "False":
                row["value"] = False
            else:
                with suppress(ValueError, TypeError):
                    row["value"] = json.loads(value)
            rows.append(row)
    return rows


async def wait_file_rows(
    path: Path,
    *,
    min_rows: int = 1,
    timeout: float = 20.0,
    match: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """等待 CSV 文件出现至少 ``min_rows`` 行（可选按 ``match`` 过滤）。"""

    async def _probe() -> list[dict[str, Any]] | None:
        rows = read_csv(path)
        if match is not None:
            rows = [row for row in rows if match(row)]
        return rows if len(rows) >= min_rows else None

    return await wait_until(
        _probe,
        timeout=timeout,
        description=f"{min_rows} rows in {path}",
    )


async def read_redis_value(address: str, key: str) -> dict[str, Any] | None:
    """用独立连接读取 Redis key 的 JSON 值；key 不存在返回 ``None``。

    连接与被测 Collector 完全独立——这是系统边界验证，不是内部状态窥探。
    """
    from tests.support.redis_client import RedisTestClient

    async with RedisTestClient(address) as client:
        raw = await client.get(key)
    return json.loads(raw) if raw is not None else None


async def wait_redis_value(
    address: str,
    key: str,
    *,
    timeout: float = 30.0,
    match: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """等待 Redis key 出现（可选按 ``match`` 过滤解码后的 JSON dict）。"""

    async def _probe() -> dict[str, Any] | None:
        value = await read_redis_value(address, key)
        if value is None:
            return None
        return value if match is None or match(value) else None

    return await wait_until(
        _probe,
        timeout=timeout,
        interval=0.2,
        description=f"redis key {key}",
    )
