"""Gateway 独立运行时：组装协议端点、Redis 镜像及 Commander。"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

import grpc

from core.infrastructure.rpc import commander_pb2_grpc

from .commands import CommandTarget, CommanderProxy
from .config import GatewaySettings
from .domain import PointDefinition, PointRole
from .modbus_codec import RegisterMapping
from .modbus_server import ModbusTcpGateway
from .point_store import PointStore
from .redis_client import RedisBatchReader
from .redis_mirror import RedisMirror, RedisPointSource

logger = logging.getLogger(__name__)


class GatewayRuntime:
    """不拥有现场 PLC 连接；仅通过 Commander 控制实际设备。"""

    def __init__(self, config: GatewaySettings) -> None:
        self._config = config
        self._channel: grpc.aio.Channel | None = None
        self._reader: RedisBatchReader | None = None
        self._mirror_task: asyncio.Task[None] | None = None
        self._servers: list[ModbusTcpGateway] = []
        self._started = False
        self._store: PointStore | None = None
        self._mirror: RedisMirror | None = None

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("Gateway already running")
        cfg = self._config
        self._channel = grpc.aio.insecure_channel(cfg.commander.target)
        targets = {
            (d.id, p.id): CommandTarget(
                p.commander_device_id or "", p.commander_point_id or "", p.commander_timeout
            )
            for d in cfg.devices
            for p in d.points
            if p.role is PointRole.COMMAND
        }
        proxy = CommanderProxy(commander_pb2_grpc.CommanderServiceStub(self._channel), targets)
        definitions = [
            PointDefinition(d.id, p.id, p.role, p.writable)
            for d in cfg.devices for p in d.points
        ]
        self._store = PointStore(
            definitions,
            command_handler=proxy if cfg.control_enabled else None,
        )
        sources = [
            RedisPointSource(
                p.redis_device_id or "", p.redis_point_id or "",
                p.key_prefix or "", p.max_age_seconds,
                target_device_id=d.id, target_point_id=p.id,
            )
            for d in cfg.devices for p in d.points
            if p.role is PointRole.FEEDBACK
        ]
        self._mirror = RedisMirror(self._store, sources)
        self._reader = RedisBatchReader(
            cfg.redis.host, cfg.redis.port,
            username=cfg.redis.username, password=cfg.redis.password,
            database=cfg.redis.database, timeout=cfg.redis.timeout,
        )
        try:
            # 数据镜像首次同步成功之前，反馈点保持不可读。
            for device in cfg.devices:
                mappings = [
                    RegisterMapping(device.id, p.id, p.area, p.address, p.data_type, p.word_order)
                    for p in device.points
                ]
                server = ModbusTcpGateway(\n                    self._store, mappings, unit_id=device.unit_id,\n                    control_client_ips=frozenset(cfg.control_client_ips),\n                )
                await server.start(device.host, device.port)
                self._servers.append(server)
            self._mirror_task = asyncio.create_task(self._refresh_loop())
            self._started = True
        except BaseException:
            await self.stop()
            raise

    async def _refresh_loop(self) -> None:
        assert self._mirror is not None
        assert self._reader is not None
        while True:
            try:
                await self._mirror.refresh(self._reader)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Gateway Redis mirror refresh failed; feedback invalidated")
            await asyncio.sleep(self._config.redis.refresh_seconds)

    async def stop(self) -> None:
        task, self._mirror_task = self._mirror_task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        for server in reversed(self._servers):
            await server.stop()
        self._servers.clear()
        if self._reader is not None:
            await self._reader.close()
            self._reader = None
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
        self._started = False
