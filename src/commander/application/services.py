"""Commander 用例服务——read / config 事务。

服务只做用例编排：固定 generation、校验输入、委托 Runtime/Dispatcher。
协议与连接细节在 DeviceSession / CommanderRuntime；配置加载在
Infrastructure 的 config adapter。即时写命令无独立用例编排，入口即
:class:`CommandDispatcher`（连接保证、幂等与错误收敛均在其中）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from core.application.port import ConfigTopic

from .config import CommanderConfig
from .errors import CommandError
from .runtime import CommanderRuntime
from .session import PointReading

# Commander 实际消费的配置主题；tasks/sinks 等无关文件变更不应
# 影响本进程的配置一致性检查（外部 expected hash 契约仍按全目录）。
COMMANDER_CONFIG_TOPICS: tuple[ConfigTopic, ...] = (
    ConfigTopic.SYSTEM,
    ConfigTopic.DEVICE_MODELS,
    ConfigTopic.DEVICES,
    ConfigTopic.POINTS,
    ConfigTopic.UNITS,
)


class CommanderReadService:
    """按设备和 point_id 执行即时读取。"""

    def __init__(self, runtime: CommanderRuntime) -> None:
        self._runtime = runtime

    async def read_point(self, device_id: str, point_id: str) -> PointReading:
        """读取单点并返回工程值。"""
        values = await self.read_points(device_id, [point_id])
        return values[0]

    async def read_points(
        self,
        device_id: str,
        point_ids: list[str],
    ) -> list[PointReading]:
        """一次连接保证后批量读取多个点并返回工程值。

        Raises:
            CommandError: 设备未知、点位为空/未知或设备未连接。
        """
        async with self._runtime.operation():
            try:
                device = self._runtime.device(device_id)
            except KeyError as exc:
                raise CommandError(f"unknown device '{device_id}'") from exc

            if not point_ids:
                raise CommandError("point_ids must be non-empty")

            unknown = [
                point_id for point_id in point_ids if point_id not in device.point_table.points
            ]
            if unknown:
                raise CommandError(f"unknown points on device '{device_id}': {unknown}")
            if not await self._runtime.ensure_connected(device_id):
                raise CommandError(f"device '{device_id}' is not connected")

            readings = await device.read_points(point_ids)
            by_id = {reading.point_id: reading for reading in readings}
            missing_values = [point_id for point_id in point_ids if point_id not in by_id]
            if missing_values:
                raise CommandError(
                    f"device '{device_id}' returned no values for points " f"{missing_values}"
                )
            return [by_id[point_id] for point_id in point_ids]


class CommanderConfigService:
    """Commander 配置事务入口。

    拥有 prepare / activate / abort 两阶段事务的候选加载与完整性校验：
    fingerprint 前后快照防 TOCTOU、候选配置加载、expected hash 比对。
    generation 状态机本身由 CommanderRuntime 持有。

    配置加载函数与指纹函数由组合根注入（实现位于 Infrastructure 的
    config adapter），本服务不直接依赖 Infrastructure。

    ``fingerprint`` 是全目录指纹（跨进程 expected hash 契约）；
    ``consistency_fingerprint`` 只覆盖 Commander 消费的主题，用于
    prepare 期间的前后一致性（TOCTOU）检查。
    """

    def __init__(
        self,
        config_dir: Path,
        runtime: CommanderRuntime,
        *,
        load_config: Callable[[Path], CommanderConfig],
        fingerprint: Callable[[Path], str],
        consistency_fingerprint: Callable[[Path], str],
    ) -> None:
        self._config_dir = config_dir
        self._runtime = runtime
        self._load_config = load_config
        self._fingerprint = fingerprint
        self._consistency_fingerprint = consistency_fingerprint

    async def prepare_config(self, revision_id: str, expected_config_hash: str) -> str:
        """加载候选配置并构造 prepared generation，返回实际配置指纹。"""
        before_hash = self._consistency_fingerprint(self._config_dir)
        candidate = self._load_config(self._config_dir)
        after_hash = self._consistency_fingerprint(self._config_dir)
        if before_hash != after_hash:
            raise ValueError(
                "config changed while preparing: " f"before={before_hash} after={after_hash}"
            )
        actual_hash = self._fingerprint(self._config_dir)
        if actual_hash != expected_config_hash:
            raise ValueError(
                "config hash mismatch: " f"expected={expected_config_hash} actual={actual_hash}"
            )
        await self._runtime.prepare_config(revision_id, candidate, actual_hash)
        return actual_hash

    async def activate_config(self, revision_id: str) -> None:
        """激活指定 prepared revision。"""
        await self._runtime.activate_config(revision_id)

    async def abort_config(self, revision_id: str) -> bool:
        """幂等撤销指定 prepared revision。"""
        return await self._runtime.abort_config(revision_id)
