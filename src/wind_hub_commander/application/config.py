"""Commander 配置事务服务。

拥有 prepare / activate / abort 两阶段事务的候选加载与完整性校验：
fingerprint 前后快照防 TOCTOU、候选配置加载、expected hash 比对。
generation 状态机本身由 CommanderRuntime 持有；gRPC adapter 只做协议转换后
委托本服务，不直接感知配置加载细节。
"""

from __future__ import annotations

from pathlib import Path

from wind_hub_commander.config import load_commander_config
from wind_hub_commander.runtime import CommanderRuntime
from wind_hub_core.config import fingerprint_config_set


class CommanderConfigService:
    """Commander 配置事务入口。"""

    def __init__(self, config_dir: Path, runtime: CommanderRuntime) -> None:
        self._config_dir = config_dir
        self._runtime = runtime

    async def prepare_config(self, revision_id: str, expected_config_hash: str) -> str:
        """加载候选配置并构造 prepared generation，返回实际配置指纹。"""
        before_hash = fingerprint_config_set(self._config_dir)
        candidate = load_commander_config(self._config_dir)
        actual_hash = fingerprint_config_set(self._config_dir)
        if before_hash != actual_hash:
            raise ValueError(
                "config changed while preparing: "
                f"before={before_hash} after={actual_hash}"
            )
        if actual_hash != expected_config_hash:
            raise ValueError(
                "config hash mismatch: "
                f"expected={expected_config_hash} actual={actual_hash}"
            )
        await self._runtime.prepare_config(revision_id, candidate, actual_hash)
        return actual_hash

    async def activate_config(self, revision_id: str) -> None:
        """激活指定 prepared revision。"""
        await self._runtime.activate_config(revision_id)

    async def abort_config(self, revision_id: str) -> bool:
        """幂等撤销指定 prepared revision。"""
        return await self._runtime.abort_config(revision_id)
