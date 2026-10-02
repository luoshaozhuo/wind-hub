"""Commander 设备会话运行时。

Runtime 持有 DeviceSession 注册表并按需建立连接。它没有周期任务、采集循环、
Sink 或后台调度，只为即时 read/write/diagnostic 提供连接生命周期。
"""

from __future__ import annotations

import asyncio
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
        items = list(self.devices.items())
        results = await asyncio.gather(
            *(device.close() for _device_id, device in items),
            return_exceptions=True,
        )
        for (device_id, _device), result in zip(items, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "Commander 关闭设备会话失败 device=%s error=%s",
                    device_id,
                    result,
                )

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
                    timeout=self.config.connect_timeout,
                )
            except Exception as exc:
                logger.warning(
                    "Commander 设备连接失败 device=%s error=%s",
                    device_id,
                    exc,
                )
                return False
            return device.health().healthy
