"""System / Recovery 测试的统一等待原语——全部是带超时的轮询，禁止硬 sleep。

每个 ``wait_*`` 在条件满足时返回观测到的数据，超时抛 :class:`WaitTimeoutError`
并附最近一次观测快照，保证失败报告可读（而不是一句 ``assert False``）。
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable
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

    通过现行 CommonService 的真实 ``GetServiceStatus`` RPC 判定（而非仅
    TCP 可连——gRPC 监听先于 Runtime 启动完成，仅探测端口会拿到未就绪的
    Collector）。该 RPC 不属于管理员面，无需令牌。
    """

    async def _probe() -> str | None:
        import grpc
        from google.protobuf import json_format

        from contracts import common_pb2, common_pb2_grpc

        channel = grpc.aio.insecure_channel(target)
        try:
            stub = common_pb2_grpc.CommonServiceStub(channel)
            response = await stub.GetServiceStatus(common_pb2.Empty(), timeout=2.0)
            return json_format.MessageToJson(response)
        finally:
            await channel.close()

    return await wait_until(
        _probe,
        timeout=timeout,
        interval=0.2,
        description=f"grpc ready at {target}",
    )


async def wait_commander_ready(
    target: str,
    *,
    ca_cert: bytes,
    client_cert: bytes,
    client_key: bytes,
    timeout: float = 30.0,
) -> str:
    """以真实双向 TLS 身份调用 GetServiceStatus，拒绝明文探针伪就绪。"""

    async def _probe() -> str | None:
        import grpc

        from contracts import common_pb2, common_pb2_grpc

        credentials = grpc.ssl_channel_credentials(
            root_certificates=ca_cert,
            private_key=client_key,
            certificate_chain=client_cert,
        )
        channel = grpc.aio.secure_channel(target, credentials)
        try:
            status = await common_pb2_grpc.CommonServiceStub(channel).GetServiceStatus(
                common_pb2.Empty(), timeout=2.0
            )
            return status.health or None
        except grpc.aio.AioRpcError as exc:
            if exc.code() is grpc.StatusCode.UNAVAILABLE:
                return None
            raise
        finally:
            await channel.close()

    return await wait_until(
        _probe,
        timeout=timeout,
        interval=0.2,
        description=f"authenticated commander grpc ready at {target}",
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


async def wait_tcp_port(
    host: str,
    port: int,
    *,
    timeout: float = 20.0,
) -> str:
    """等待 TCP 端口可连（用于无健康 RPC 的进程就绪探针，如 sink_service）。"""

    async def _probe() -> str | None:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=1.0)
        except OSError:
            return None
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return f"{host}:{port}"

    return await wait_until(_probe, timeout=timeout, interval=0.2, description=f"tcp {host}:{port}")


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
