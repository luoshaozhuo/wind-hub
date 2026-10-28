"""RedisSink RESP2 协议行为测试（本机脚本化伪 Redis，零外部依赖）。

通过 127.0.0.1 回环上的极简 RESP 脚本服务器验证：
- open 握手：AUTH（ACL/仅密码）/SELECT/PING 顺序与幂等；
- 失败回复/连接拒绝转 SinkError 并断连；
- EVAL CAS：1 成功、0 过期接受、其他整数与 -ERR 失败断连；
- 非有限值剔除、write_task 白名单与 key 前缀；
- health PING 状态与 close 语义。
"""

from __future__ import annotations

import asyncio
import json
import math

import pytest

from core.application import Quality
from core.application.sink_contract import ResolvedSink
from core.application.sink_error import SinkError
from core.domain import Sink
from core.domain.point_value import PointValue
from core.infrastructure.sink.redis import RedisSink


def _parse_commands(buffer: bytes) -> tuple[list[list[bytes]], int]:
    """解析完整 RESP2 数组命令，返回命令列表与已消费字节数。"""
    index = 0
    commands: list[list[bytes]] = []
    try:
        while index < len(buffer):
            newline = buffer.index(b"\r\n", index)
            count = int(buffer[index + 1 : newline])
            index = newline + 2
            arguments: list[bytes] = []
            for _ in range(count):
                newline = buffer.index(b"\r\n", index)
                length = int(buffer[index + 1 : newline])
                index = newline + 2
                arguments.append(buffer[index : index + length])
                index += length + 2
            commands.append(arguments)
    except (ValueError, IndexError):
        return commands, index
    return commands, index


class FakeRedisServer:
    def __init__(self, responder) -> None:
        self._responder = responder
        self.commands: list[list[bytes]] = []
        self._server: asyncio.AbstractServer | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        buffer = b""
        consumed = 0
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buffer = buffer[consumed:] + chunk
                commands, consumed = _parse_commands(buffer)
                for command in commands:
                    self.commands.append(command)
                    writer.write(self._responder(command))
                    await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()


def _simple_responder(command: list[bytes]) -> bytes:
    name = command[0].upper()
    if name == b"PING":
        return b"+PONG\r\n"
    if name == b"EVAL":
        return b":1\r\n"
    return b"+OK\r\n"


def _sink(port: int, **connection_overrides) -> RedisSink:
    connection = {"host": "127.0.0.1", "port": port, "timeout": 2.0}
    connection.update(connection_overrides)
    return RedisSink(Sink(sink_id="redis-1", kind="redis", enabled=True, connection=connection))


def _pv(value=8.5, *, device="wt01", point="wind_speed", quality=Quality.GOOD) -> PointValue:
    return PointValue(device_id=device, point_id=point, value=value, quality=quality)


def _command_names(server: FakeRedisServer) -> list[str]:
    return [command[0].decode().upper() for command in server.commands]


@pytest.fixture
async def fake_redis():
    server = FakeRedisServer(_simple_responder)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


async def test_open_handshake_order_and_idempotence(fake_redis) -> None:
    sink = _sink(fake_redis.port, username="scada", password="secret", database=3)

    await sink.open()
    await sink.open()

    names = _command_names(fake_redis)
    assert names == ["AUTH", "SELECT", "PING"]
    auth_args = fake_redis.commands[0][1:]
    assert auth_args == [b"scada", b"secret"]
    assert fake_redis.commands[1] == [b"SELECT", b"3"]

    await sink.close()


async def test_open_with_password_only(fake_redis) -> None:
    sink = _sink(fake_redis.port, password="secret")
    await sink.open()
    assert _command_names(fake_redis) == ["AUTH", "PING"]
    assert fake_redis.commands[0] == [b"AUTH", b"secret"]
    await sink.close()


async def test_open_error_reply_becomes_sink_error(fake_redis) -> None:
    fake_redis._responder = lambda command: b"-WRONGPASS invalid\r\n"
    sink = _sink(fake_redis.port, password="bad")

    with pytest.raises(SinkError, match="Redis open failed"):
        await sink.open()

    health = await sink.health()
    assert health.healthy is False
    assert "WRONGPASS" in (health.message or "")


async def test_open_connection_refused() -> None:
    # 先占一个端口再关闭，尽量保证连接被拒绝而不是超时。
    listener = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    listener.close()
    await listener.wait_closed()

    sink = _sink(port, timeout=1.0)
    with pytest.raises(SinkError, match="Redis open failed"):
        await sink.open()


async def test_open_rejects_unexpected_reply_type(fake_redis) -> None:
    fake_redis._responder = lambda command: b"$4\r\ngarb\r\n"
    sink = _sink(fake_redis.port)
    with pytest.raises(SinkError, match="Redis open failed"):
        await sink.open()


async def test_write_uses_eval_cas_and_accepts_stale_zero(fake_redis) -> None:
    replies = iter([b"+PONG\r\n", b":1\r\n", b":0\r\n"])
    fake_redis._responder = lambda command: next(replies)
    sink = _sink(fake_redis.port)
    await sink.open()

    await sink.write([_pv(), _pv(9.0)])  # 第二个假装过期被拒

    eval_commands = [c for c in fake_redis.commands if c[0] == b"EVAL"]
    assert len(eval_commands) == 2
    # EVAL <CAS 脚本> 1 <key> <payload>：单 key、payload 是 strict JSON。
    for command in eval_commands:
        assert command[1].startswith(b"local current=")
        assert command[2] == b"1"
        key, payload = command[3], command[4]
        assert key.startswith(b"wind-hub")
        json.loads(payload)  # strict JSON
    await sink.close()


async def test_write_before_open_is_rejected(fake_redis) -> None:
    sink = _sink(fake_redis.port)
    with pytest.raises(SinkError, match="not connected"):
        await sink.write([_pv()])


async def test_write_unexpected_integer_disconnects(fake_redis) -> None:
    replies = iter([b"+PONG\r\n", b":2\r\n"])
    fake_redis._responder = lambda command: next(replies)
    sink = _sink(fake_redis.port)
    await sink.open()

    with pytest.raises(SinkError, match="Redis write failed"):
        await sink.write([_pv()])

    # 失败后连接已拆除，health 不重连。
    health = await sink.health()
    assert health.healthy is False
    with pytest.raises(SinkError, match="not connected"):
        await sink.write([_pv()])


async def test_write_error_reply_disconnects(fake_redis) -> None:
    replies = iter([b"+PONG\r\n", b"-ERR server broken\r\n"])
    fake_redis._responder = lambda command: next(replies)
    sink = _sink(fake_redis.port)
    await sink.open()

    with pytest.raises(SinkError, match="Redis write failed"):
        await sink.write([_pv()])


async def test_non_finite_values_are_dropped(fake_redis) -> None:
    sink = _sink(fake_redis.port)
    await sink.open()

    await sink.write([_pv(float("nan")), _pv(math.inf)])

    assert not any(command[0] == b"EVAL" for command in fake_redis.commands)
    await sink.close()


async def test_write_task_filters_sources_and_uses_prefix(fake_redis) -> None:
    resolved = ResolvedSink(
        sink_id="redis-1",
        kind="redis",
        enabled=True,
        connection={"host": "127.0.0.1", "port": fake_redis.port, "timeout": 2.0},
        task_sources={"task-a": (("wt01", "wind_speed"),)},
        task_key_prefixes={"task-a": "scada"},
    )
    sink = RedisSink(resolved)
    await sink.open()

    await sink.write_task("task-a", [_pv(), _pv(1.0, device="wt01", point="rotor_rpm")])
    await sink.write_task("task-unknown", [_pv()])

    eval_commands = [c for c in fake_redis.commands if c[0] == b"EVAL"]
    assert len(eval_commands) == 1
    # EVAL script 1 key payload —— key 在 ARGV 前的 KEYS 位置（第 3 个参数）。
    key = eval_commands[0][3].decode()
    assert key.startswith("scada")
    await sink.close()


async def test_empty_batch_is_noop(fake_redis) -> None:
    sink = _sink(fake_redis.port)
    await sink.open()
    await sink.write([])
    assert _command_names(fake_redis) == ["PING"]
    await sink.close()


async def test_health_ping_states(fake_redis) -> None:
    sink = _sink(fake_redis.port)
    health = await sink.health()
    assert health.healthy is False

    await sink.open()
    assert (await sink.health()).healthy is True

    fake_redis._responder = lambda command: b"+NOPE\r\n"
    health = await sink.health()
    assert health.healthy is False
    assert "NOPE" in (health.message or "")

    await sink.close()
    health = await sink.health()
    assert health.healthy is False
    assert health.message == "closed"
