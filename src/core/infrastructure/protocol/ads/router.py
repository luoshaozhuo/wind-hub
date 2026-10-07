"""进程级 ADS 本机 AMS 身份。

本模块只初始化本机 pyads router identity，不自动向远端 PLC 创建/修复 AMS route。
生命周期由 Composition Root 显式持有。
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

from core.application import ConfigError, ProtocolError


@dataclass(frozen=True, slots=True)
class ADSLocalConfig:
    """当前进程的本机 ADS 身份。"""

    local_ams_net_id: str
    local_ip: str

    def __post_init__(self) -> None:
        net_id = self.local_ams_net_id.strip()
        local_ip = self.local_ip.strip()
        if not _is_valid_ams_net_id(net_id):
            raise ConfigError(
                f"invalid local AMS Net ID '{self.local_ams_net_id}'"
            )
        if not local_ip:
            raise ConfigError("ADS local_ip must not be empty")
        object.__setattr__(self, "local_ams_net_id", net_id)
        object.__setattr__(self, "local_ip", local_ip)


class ADSLocalRouter:
    """pyads 进程级本机 router 端口生命周期 owner。

    一个进程应只创建一个实例，并在所有 ADS DeviceSession 之前 initialize，
    在全部 ADS session 关闭后 close。
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._config: ADSLocalConfig | None = None
        self._initialized = False

    @property
    def initialized(self) -> bool:
        """是否已成功初始化本机 ADS port。"""
        return self._initialized

    @property
    def config(self) -> ADSLocalConfig | None:
        """返回当前已生效本机 ADS 配置。"""
        return self._config

    async def initialize(self, config: ADSLocalConfig) -> None:
        """幂等初始化；不同配置重复初始化属于部署错误。"""
        async with self._lock:
            if self._initialized:
                if self._config != config:
                    raise ConfigError(
                        "ADS local router is already initialized with a "
                        "different local identity"
                    )
                return

            pyads = _pyads()
            try:
                await asyncio.to_thread(pyads.open_port)
                await asyncio.to_thread(
                    pyads.set_local_address,
                    config.local_ams_net_id,
                )
            except Exception as exc:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(pyads.close_port)
                raise ProtocolError(
                    f"failed to initialize local ADS identity "
                    f"'{config.local_ams_net_id}': {exc}"
                ) from exc

            self._config = config
            self._initialized = True

    async def close(self) -> None:
        """关闭本进程打开的 pyads port；重复调用安全。"""
        async with self._lock:
            if not self._initialized:
                return
            pyads = _pyads()
            try:
                await asyncio.to_thread(pyads.close_port)
            finally:
                self._initialized = False
                self._config = None


def _pyads() -> Any:
    try:
        import pyads  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ProtocolError(
            "ADS support requires the optional 'pyads' dependency"
        ) from exc
    return pyads


def _is_valid_ams_net_id(value: str) -> bool:
    parts = value.split(".")
    return len(parts) == 6 and all(
        part.isdigit() and 0 <= int(part) <= 255
        for part in parts
    )
