"""Commander 设备会话运行时与 generation 生命周期管理。

Runtime 持有按 generation 管理的 DeviceSession 注册表。read/write/diagnostic
进入时固定当前 generation；配置激活只做原子 generation 切换，旧 generation
由后台 retirement task 等待在途操作排空后关闭——RPC deadline 不影响资源回收。

prepare / activate / abort 语义与旧 Commander 一致：

- prepare 构造完整候选 generation（不执行网络 I/O），替换旧候选；
- activate 原子切换 active generation，旧 generation 异步排空回收；
- abort 幂等撤销候选；stop 回收 active/prepared/retired 全部会话。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import MappingProxyType

from core.infrastructure import ProtocolRegistry

from .config import CommanderConfig
from .session import DeviceSession

logger = logging.getLogger(__name__)

#: 组合根注入的协议环境准备钩子（如 ADS 本机 identity 初始化）。
#: 失败由 Runtime 记录并降级（ADS 操作不可用，其他协议继续服务）。
ProtocolPrepareHook = Callable[[CommanderConfig], Awaitable[None]]

#: 协议环境「尚未准备」哨兵——与「已按 ads_local=None 准备」区分。
_NEVER_PREPARED: object = object()


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
    """Commander 设备会话注册表与 generation 生命周期管理器。

    Args:
        config: 启动配置。
        config_hash: 启动配置集指纹。
        protocol_registry: 组合根显式构造的协议 Driver 注册表。
        prepare_protocols: 可选协议环境准备钩子；activate/start 后在当前
            generation 上执行一次（失败降级，不阻断）。
    """

    def __init__(
        self,
        config: CommanderConfig,
        *,
        config_hash: str,
        protocol_registry: ProtocolRegistry,
        prepare_protocols: ProtocolPrepareHook | None = None,
    ) -> None:
        self._protocol_registry = protocol_registry
        self._prepare_protocols = prepare_protocols
        self._reload_lock = asyncio.Lock()
        self._operation_generation: ContextVar[_Generation | None] = ContextVar(
            "commander_operation_generation",
            default=None,
        )
        self._current = self._build_generation(config)
        # 协议环境准备按 ADS 本机身份记账：身份是 restart-required 的进程级
        # 配置，同一身份只 initialize 一次；sentinel 区分「尚未准备」与
        # 「已按无 ADS 身份准备」。
        self._protocols_ready_for: object = _NEVER_PREPARED
        self._active_revision = "startup"
        self._active_config_hash = config_hash
        self._prepared_revision: str | None = None
        self._prepared_config_hash: str | None = None
        self._prepared_generation: _Generation | None = None
        self._retirement_tasks: set[asyncio.Task[None]] = set()
        self._stopping = False

    @property
    def devices(self) -> Mapping[str, DeviceSession]:
        """当前激活 generation 的设备会话只读视图。

        会话注册表的唯一 owner 是各 generation；外部只能观察，配置激活
        时本视图随 ``_current`` 切换原子更新。
        """
        return MappingProxyType(self._current.devices)

    @property
    def config(self) -> CommanderConfig:
        """返回当前激活配置。"""
        return self._current.config

    @property
    def active_revision(self) -> str:
        """返回当前激活配置版本标识。"""
        return self._active_revision

    @property
    def active_config_hash(self) -> str:
        """返回当前激活 generation 对应的配置指纹。"""
        return self._active_config_hash

    @property
    def prepared_revision(self) -> str | None:
        """返回当前已准备但尚未激活的配置版本。"""
        return self._prepared_revision

    @property
    def prepared_config_hash(self) -> str | None:
        """返回当前 prepared generation 对应的配置指纹。"""
        return self._prepared_config_hash

    def _build_generation(self, config: CommanderConfig) -> _Generation:
        """基于候选配置构造完整 generation，不执行网络 I/O。"""
        devices: dict[str, DeviceSession] = {}
        locks: dict[str, asyncio.Lock] = {}
        for device in config.devices.values():
            point_table = config.point_table_for_device(device.device_id)
            protocol = self._protocol_registry.create(
                device.endpoint,
                point_table,
                config.device_options_for(device.device_id),
            )
            devices[str(device.device_id)] = DeviceSession(
                device,
                point_table,
                config.business_points,
                config.point_meta.get(point_table.point_table_id, {}),
                protocol,
            )
            locks[str(device.device_id)] = asyncio.Lock()
        return _Generation(config=config, devices=devices, connect_locks=locks)

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

    def operation_config(self) -> CommanderConfig:
        """返回当前操作固定的 generation 配置（无操作时返回最新配置）。

        设备操作（读/写/诊断）必须在 ``operation()`` 内读取本配置——
        generation 固定期间 activate 新配置不影响本次操作的超时与协议参数。
        """
        return self._generation().config

    async def reload(
        self,
        config: CommanderConfig,
        *,
        config_hash: str,
        revision_id: str,
    ) -> None:
        """本地兼容入口：按 prepare + activate 原子切换配置代次。"""
        await self.prepare_config(revision_id, config, config_hash)
        await self.activate_config(revision_id)

    async def prepare_config(
        self,
        revision_id: str,
        config: CommanderConfig,
        config_hash: str,
    ) -> None:
        """构造并保存候选 generation，不影响当前运行配置。

        Raises:
            ValueError: revision_id/config_hash 为空，或候选配置的进程级
                ADS 本机身份与当前配置不一致——ADS local identity 是
                restart-required 的启动配置，热重载拒绝其任何变化。
        """
        if not revision_id:
            raise ValueError("revision_id must not be empty")
        if not config_hash:
            raise ValueError("config_hash must not be empty")
        if config.ads_local != self._current.config.ads_local:
            raise ValueError("ADS local identity change requires process restart")
        candidate = self._build_generation(config)
        async with self._reload_lock:
            if self._stopping:
                await self._close_candidate(candidate, reason="prepare-during-stop")
                raise RuntimeError("Commander is stopping")
            previous = self._prepared_generation
            self._prepared_generation = candidate
            self._prepared_revision = revision_id
            self._prepared_config_hash = config_hash
            if previous is not None:
                await self._close_candidate(previous, reason="replace-prepared")

    async def activate_config(self, revision_id: str) -> None:
        """原子激活 prepared generation，并异步回收旧 generation。

        返回即表示 active generation、revision 与 config hash 已完成切换；旧会话
        的排空与关闭不属于 Activate RPC 的完成条件。
        """
        async with self._reload_lock:
            if self._stopping:
                raise RuntimeError("Commander is stopping")
            if self._prepared_revision != revision_id:
                raise ValueError(
                    f"prepared revision mismatch: expected={self._prepared_revision!r} "
                    f"requested={revision_id!r}"
                )
            if self._prepared_generation is None or self._prepared_config_hash is None:
                raise ValueError("no prepared configuration")

            old_generation = self._current
            new_generation = self._prepared_generation
            self._current = new_generation
            self._active_revision = revision_id
            self._active_config_hash = self._prepared_config_hash
            self._prepared_revision = None
            self._prepared_config_hash = None
            self._prepared_generation = None
            self._schedule_retirement(old_generation, reason="activate")

        await asyncio.shield(self.start())

    async def abort_config(self, revision_id: str) -> bool:
        """幂等撤销指定 prepared revision，不修改当前 active generation。

        Returns:
            实际找到并撤销对应候选 generation 时返回 True；revision 不匹配或
            当前没有候选配置时返回 False。
        """
        async with self._reload_lock:
            if self._prepared_revision != revision_id:
                return False
            generation = self._prepared_generation
            self._prepared_revision = None
            self._prepared_config_hash = None
            self._prepared_generation = None
            if generation is not None:
                await self._close_candidate(generation, reason="abort-prepared")
        return generation is not None

    async def start(self) -> None:
        """执行当前 generation 的协议环境准备（如 ADS 本机初始化）。

        不主动连接所有设备——设备连接在首次操作时 lazy 建立。准备失败
        只记录并降级：受影响协议的操作不可用，其他协议继续服务。

        ADS 本机身份是 restart-required 的进程级配置：同一身份只准备
        一次（进程 start 时），reload 激活不重复 initialize；准备失败
        不记账，下一次 start 仍会重试。
        """
        if self._prepare_protocols is None:
            return
        identity = self._current.config.ads_local
        if identity == self._protocols_ready_for:
            return
        try:
            await self._prepare_protocols(self._current.config)
        except Exception:
            logger.warning(
                "Commander 协议环境准备失败；受影响协议将保持不可用，其他协议继续服务",
                exc_info=True,
            )
        else:
            self._protocols_ready_for = identity

    async def stop(self) -> None:
        """停止 generation 变更并回收 active/prepared/retired 全部会话。"""
        async with self._reload_lock:
            self._stopping = True
            current = self._current
            prepared = self._prepared_generation
            self._prepared_revision = None
            self._prepared_config_hash = None
            self._prepared_generation = None

        if prepared is not None:
            self._schedule_retirement(prepared, reason="stop-prepared")
        self._schedule_retirement(current, reason="stop-active")

        tasks = list(self._retirement_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule_retirement(
        self,
        generation: _Generation,
        *,
        reason: str,
    ) -> None:
        """把 generation 清理注册为进程级后台任务，并持有到任务结束。"""
        task = asyncio.create_task(
            self._retire_generation(generation, reason=reason),
            name=f"commander-retire-{reason}",
        )
        self._retirement_tasks.add(task)
        task.add_done_callback(self._retirement_tasks.discard)

    async def _close_candidate(
        self,
        generation: _Generation,
        *,
        reason: str,
    ) -> None:
        """同步关闭从未服务过操作的候选 generation。

        prepared generation 从未进入 active（``active_operations`` 恒为 0，
        无需排空），同步关闭使 prepare 拒绝 / abort / 候选替换路径的资源
        回收是确定性的，不遗留依赖事件循环存活的后台任务。
        """
        await self._close_generation(generation, reason=reason)

    async def _retire_generation(
        self,
        generation: _Generation,
        *,
        reason: str,
    ) -> None:
        """等待 generation 在途操作排空后关闭全部设备会话。"""
        await generation.drained.wait()
        await self._close_generation(generation, reason=reason)

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
        """确保固定 generation 中的设备连接可用。

        并发 ensure 由 per-device lock 串行化；重连前先尽力关闭旧会话，
        connect 应用配置级超时；失败返回 False 而不抛出。
        """
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
