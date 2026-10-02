"""Commander 设备会话运行时。

Runtime 持有按 generation 管理的 DeviceSession 注册表。read/write/diagnostic
进入时固定当前 generation；reload 原子切换新 generation，并等待旧 generation
在途操作自然结束后再关闭旧会话。
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import AsyncIterator

from wind_hub_commander.config import CommanderConfig
from wind_hub_core.device.session import DeviceSession
from wind_hub_core.protocol import protocol_registry

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Generation:
    """一代 Commander 配置对应的会话集合。"""

    config: CommanderConfig
    devices: dict[str, DeviceSession]
    connect_locks: dict[str, asyncio.Lock]
    active_operations: int = 0
    drained: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.drained.set()


class CommanderRuntime:
    """Commander 设备会话注册表与 generation 生命周期管理器。"""

    def __init__(self, config: CommanderConfig) -> None:
        self._reload_lock = asyncio.Lock()
        self._operation_generation: ContextVar[_Generation | None] = ContextVar(
            "commander_operation_generation",
            default=None,
        )
        self._current = self._build_generation(config)
        self._active_revision = "startup"
        self._prepared_revision: str | None = None
        self._prepared_generation: _Generation | None = None
        # 保持原有 devices 映射对象引用稳定，供状态查询等只读代码使用。
        self.devices: dict[str, DeviceSession] = {}
        self.devices.update(self._current.devices)

    @property
    def config(self) -> CommanderConfig:
        """返回当前激活配置。"""
        return self._current.config

    @property
    def active_revision(self) -> str:
        """返回当前激活配置版本标识。"""
        return self._active_revision

    @property
    def prepared_revision(self) -> str | None:
        """返回当前已准备但尚未激活的配置版本。"""
        return self._prepared_revision

    def _build_generation(self, config: CommanderConfig) -> _Generation:
        """基于候选配置构造完整 generation，不执行网络 I/O。"""
        devices: dict[str, DeviceSession] = {}
        locks: dict[str, asyncio.Lock] = {}
        for device_config in config.devices.devices:
            protocol = protocol_registry.create(device_config.protocol, device_config)
            devices[device_config.device_id] = DeviceSession(
                config=device_config,
                points=config.points_for_device(device_config.device_id),
                protocol=protocol,
            )
            locks[device_config.device_id] = asyncio.Lock()
        return _Generation(
            config=config,
            devices=devices,
            connect_locks=locks,
        )

    @asynccontextmanager
    async def operation(self) -> AsyncIterator[None]:
        """固定当前 generation，直到本次设备操作结束。"""
        inherited = self._operation_generation.get()
        if inherited is not None:
            yield
            return

        generation = self._current
        generation.active_operations += 1
        if generation.active_operations == 1:
            generation.drained.clear()
        token = self._operation_generation.set(generation)
        try:
            yield
        finally:
            self._operation_generation.reset(token)
            generation.active_operations -= 1
            if generation.active_operations == 0:
                generation.drained.set()

    def _generation(self) -> _Generation:
        """返回当前操作固定的 generation，否则返回最新 generation。"""
        return self._operation_generation.get() or self._current

    async def prepare_config(
        self,
        revision_id: str,
        config: CommanderConfig,
    ) -> None:
        """构造并保存候选 generation，不影响当前运行配置。"""
        if not revision_id:
            raise ValueError("revision_id must not be empty")
        candidate = self._build_generation(config)
        async with self._reload_lock:
            previous = self._prepared_generation
            self._prepared_generation = candidate
            self._prepared_revision = revision_id

        if previous is not None:
            await previous.drained.wait()
            await self._close_generation(previous, reason="replace-prepared")

    async def activate_config(self, revision_id: str) -> None:
        """激活已准备的 generation，并排空后关闭旧 generation。"""
        async with self._reload_lock:
            if self._prepared_revision != revision_id:
                raise ValueError(
                    f"prepared revision mismatch: expected={self._prepared_revision!r} "
                    f"requested={revision_id!r}"
                )
            if self._prepared_generation is None:
                raise ValueError("no prepared configuration")

            old_generation = self._current
            new_generation = self._prepared_generation
            self._current = new_generation
            self._active_revision = revision_id
            self._prepared_revision = None
            self._prepared_generation = None
            self.devices.clear()
            self.devices.update(new_generation.devices)

        await self.start()
        await old_generation.drained.wait()
        await self._close_generation(old_generation, reason="activate")

    async def reload(
        self,
        config: CommanderConfig,
        *,
        revision_id: str = "legacy-reload",
    ) -> None:
        """兼容旧调用：按 prepare → activate 完成一次配置切换。"""
        await self.prepare_config(revision_id, config)
        await self.activate_config(revision_id)

    async def start(self) -> None:
        """初始化当前 generation 的进程级协议资源，不主动连接所有设备。"""
        generation = self._current
        if generation.config.ads is None:
            return
        if not any(
            device.config.protocol == "ads"
            for device in generation.devices.values()
        ):
            return
        from wind_hub_core.protocol.ads import router as ads_router

        try:
            await ads_router.ensure_local_initialized(generation.config.ads)
        except Exception:
            logger.warning(
                "Commander ADS 本机初始化失败；ADS 操作将保持不可用，其他协议继续服务",
                exc_info=True,
            )

    async def stop(self) -> None:
        """等待当前在途操作完成，再关闭当前 generation。"""
        generation = self._current
        await generation.drained.wait()
        await self._close_generation(generation, reason="stop")

    async def _close_generation(
        self,
        generation: _Generation,
        *,
        reason: str,
    ) -> None:
        """并发关闭一代设备会话；单设备失败不阻断其他资源释放。"""
        items = list(generation.devices.items())
        results = await asyncio.gather(
            *(device.close() for _device_id, device in items),
            return_exceptions=True,
        )
        for (device_id, _device), result in zip(items, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "Commander %s 关闭设备会话失败 device=%s error=%s",
                    reason,
                    device_id,
                    result,
                )

    def device(self, device_id: str) -> DeviceSession:
        """按 ID 从当前操作固定 generation 返回设备会话。"""
        generation = self._generation()
        try:
            return generation.devices[device_id]
        except KeyError as exc:
            raise KeyError(f"unknown device '{device_id}'") from exc

    async def ensure_connected(self, device_id: str) -> bool:
        """确保固定 generation 中的设备连接可用。"""
        generation = self._generation()
        try:
            device = generation.devices[device_id]
            lock = generation.connect_locks[device_id]
        except KeyError as exc:
            raise KeyError(f"unknown device '{device_id}'") from exc

        if device.health().healthy:
            return True

        async with lock:
            if device.health().healthy:
                return True
            try:
                await device.close()
            except Exception as exc:
                logger.warning(
                    "Commander 重连前关闭旧会话失败 device=%s error=%s",
                    device_id,
                    exc,
                )
            try:
                await asyncio.wait_for(
                    device.connect(),
                    timeout=generation.config.connect_timeout,
                )
            except Exception as exc:
                logger.warning(
                    "Commander 设备连接失败 device=%s error=%s",
                    device_id,
                    exc,
                )
                return False
            return device.health().healthy
