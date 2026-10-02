"""Commander 设备会话运行时。

Runtime 持有 DeviceSession 注册表并按需建立连接。它没有周期任务、采集循环、
Sink 或后台调度，只为即时 read/write/diagnostic 提供连接生命周期。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from wind_hub_commander.config import CommanderConfig
from wind_hub_core.device.session import DeviceSession
from wind_hub_core.protocol import protocol_registry

logger = logging.getLogger(__name__)


class CommanderRuntime:
    """Commander 设备会话注册表与连接生命周期管理器。"""

    def __init__(self, config: CommanderConfig) -> None:
        self.config = config
        self.devices: dict[str, DeviceSession] = {}
        self._connect_locks: dict[str, asyncio.Lock] = {}

        for device_config in config.devices.devices:
            protocol = protocol_registry.create(device_config.protocol, device_config)
            self.devices[device_config.device_id] = DeviceSession(
                config=device_config,
                points=config.points_for_device(device_config.device_id),
                protocol=protocol,
            )
            self._connect_locks[device_config.device_id] = asyncio.Lock()

    async def start(self) -> None:
        """初始化进程级协议资源，不主动连接所有设备。"""
        if self.config.ads is None:
            return
        if not any(device.config.protocol == "ads" for device in self.devices.values()):
            return
        from wind_hub_core.protocol.ads import router as ads_router

        try:
            await ads_router.ensure_local_initialized(self.config.ads)
        except Exception:
            logger.warning(
                "Commander ADS 本机初始化失败；ADS 操作将保持不可用，其他协议继续服务",
                exc_info=True,
            )

    async def stop(self) -> None:
        """并发关闭全部设备会话；单设备关闭失败不阻断其余资源释放。"""
        results = await asyncio.gather(
            *(device.close() for device in self.devices.values()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                # stop 是清理边界；全部资源都已尝试关闭，不在这里隐藏后续清理。
                continue

    def device(self, device_id: str) -> DeviceSession:
        """按 ID 返回设备会话。"""
        try:
            return self.devices[device_id]
        except KeyError as exc:
            raise KeyError(f"unknown device '{device_id}'") from exc

    async def ensure_connected(self, device_id: str) -> bool:
        """确保指定设备连接可用；同设备并发连接由锁串行化。"""
        device = self.device(device_id)
        if device.health().healthy:
            return True

        lock = self._connect_locks[device_id]
        async with lock:
            if device.health().healthy:
                return True
            with contextlib.suppress(Exception):
                await device.close()
            try:
                await asyncio.wait_for(
                    device.connect(),
                    timeout=self.config.connect_timeout,
                )
            except Exception:
                return False
            return device.health().healthy
