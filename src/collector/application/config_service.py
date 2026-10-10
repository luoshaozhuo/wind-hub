"""Collector 配置热重载编排服务（Application 层）。

职责边界：本服务只做「load → validate → diff → runtime.reconfigure →
commit current config」的编排；具体的设备/sink 增删重建、Task Instance
重新展开与点表重注入全部由 :meth:`CollectorRuntime.reconfigure` 执行——
本服务不直接触碰任何运行时组件。

配置加载与目录摘要计算是 Infrastructure 关注点，经构造注入的可调用对象
完成（与 Commander 同一模式）——Application 层不 import Infrastructure。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .config import CollectorConfig
from .reload import (
    ConfigDiff,
    ReloadResult,
    compute_diff,
    restart_required_runtime_fields,
)
from .runtime import CollectorRuntime

logger = logging.getLogger(__name__)

#: 配置加载器——Infrastructure 注入；加载失败抛 ConfigError。
ConfigLoader = Callable[[], CollectorConfig]

#: 配置目录摘要——Infrastructure 注入（TOCTOU 校验与 RPC config_hash 契约）。
ConfigDigest = Callable[[], str]


class CollectorConfigService:
    """配置热重载编排服务。

    ``reload()`` 编排热重载，``current_config`` 暴露当前配置。

    ``reload()`` 流程：

    1. 从磁盘加载新配置（load + 校验，失败即中止，不应用任何改动）；
    2. 计算 :class:`ConfigDiff`（无变更则直接返回成功）；
    3. 调用 :meth:`CollectorRuntime.reconfigure` 执行全部运行时重构；
    4. 仅当全部运行时重构成功时，提交新配置为当前快照。

    reconfigure 返回的错误列表原样汇入 ``ReloadResult.errors``。部分失败时
    ``success=False`` 且成功基线保持不变；下一次 reload 会重新计算同一 diff
    并重试。CollectorRuntime 的重构路径必须保持可重复调用。
    """

    def __init__(
        self,
        runtime: CollectorRuntime,
        current_config: CollectorConfig,
        *,
        load_config: ConfigLoader,
        config_digest: ConfigDigest,
        config_hash: str,
    ) -> None:
        self._runtime = runtime
        self._load_config = load_config
        self._config_digest = config_digest
        self._config_hash = config_hash
        self._active_revision = "startup"
        self._prepared_revision: str | None = None
        self._prepared_config: CollectorConfig | None = None
        self._prepared_diff: ConfigDiff | None = None
        self._prepared_hash: str | None = None
        self._reload_lock = asyncio.Lock()
        self._local_reload_sequence = 0

        # 初始快照必须由组合根注入（assemble 启动阶段的唯一一次加载结果）
        # ——本类不自行加载，避免启动配置被重复加载、以及两次加载之间文件
        # 变化导致 CollectorRuntime 实际配置与 diff 基线不一致。
        self._current = current_config

    async def reload(self) -> ReloadResult:
        """本地一次性执行 prepare + activate 热重载。

        分布式配置事务仍通过 prepare_config/activate_config 两阶段接口；
        本方法用于 Collector 本地控制与组件测试，不绕过任何校验或
        CollectorRuntime reconfigure 语义。
        """
        self._local_reload_sequence += 1
        revision_id = f"local-{self._local_reload_sequence}"
        prepared = await self.prepare_config(revision_id)
        if not prepared.success:
            return prepared
        return await self.activate_config(revision_id)

    @property
    def current_config(self) -> CollectorConfig:
        """返回当前已提交、作为下次 diff 基线的配置快照。"""
        return self._current

    @property
    def config_hash(self) -> str:
        """当前已提交配置快照对应的配置目录摘要。"""
        return self._config_hash

    @property
    def active_revision(self) -> str:
        """返回当前已激活配置版本。"""
        return self._active_revision

    @property
    def prepared_revision(self) -> str | None:
        """返回当前已准备但尚未激活的配置版本。"""
        return self._prepared_revision

    @property
    def prepared_hash(self) -> str | None:
        """返回当前已准备配置的摘要。"""
        return self._prepared_hash

    async def prepare_config(
        self,
        revision_id: str,
        expected_config_hash: str | None = None,
        *,
        force_reconfigure: bool = False,
    ) -> ReloadResult:
        """加载并校验候选配置，校验摘要后保存候选快照。"""
        started = time.monotonic()
        if not revision_id:
            return ReloadResult(
                success=False,
                diff=ConfigDiff(),
                errors=["revision_id must not be empty"],
                duration_ms=(time.monotonic() - started) * 1000,
            )

        try:
            before_hash = self._config_digest()
            candidate = self._load_config()
            candidate_hash = self._config_digest()
            if before_hash != candidate_hash:
                raise ValueError(
                    "config changed while preparing: "
                    f"before={before_hash} after={candidate_hash}"
                )
            if expected_config_hash is not None and candidate_hash != expected_config_hash:
                raise ValueError(
                    "config hash mismatch: "
                    f"expected={expected_config_hash} actual={candidate_hash}"
                )
        except Exception as exc:
            logger.error("Prepare aborted — config load failed: %s", exc)
            return ReloadResult(
                success=False,
                diff=ConfigDiff(),
                errors=[str(exc) or type(exc).__name__],
                duration_ms=(time.monotonic() - started) * 1000,
            )

        async with self._reload_lock:
            # 不能安全热更新的配置必须在 prepare 阶段明确拒绝——「激活成功
            # 但仍使用旧值」的假激活会让上报的 revision/hash 与真实运行态
            # 不一致。force_reconfigure 只改变 diff 基线，不绕过本校验。
            rejection = self._restart_required_rejection(candidate)
            if rejection is not None:
                logger.warning(
                    "Prepare rejected revision=%s: %s",
                    revision_id,
                    "; ".join(rejection),
                )
                return ReloadResult(
                    success=False,
                    diff=ConfigDiff(),
                    errors=rejection,
                    duration_ms=(time.monotonic() - started) * 1000,
                )

            diff = (
                self._runtime.convergence_diff(candidate)
                if force_reconfigure
                else compute_diff(self._current, candidate)
            )
            self._prepared_revision = revision_id
            self._prepared_config = candidate
            self._prepared_diff = diff
            self._prepared_hash = candidate_hash

        logger.info(
            "Prepared config revision=%s changed=%s force_reconfigure=%s",
            revision_id,
            diff.has_any_changes,
            force_reconfigure,
        )
        return ReloadResult(
            success=True,
            diff=diff,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    def _restart_required_rejection(self, candidate: CollectorConfig) -> list[str] | None:
        """返回候选配置中的 restart-required 变化；无变化时返回 None。

        RuntimeParams 的 restart-required 字段与进程级 ADS 本机身份都不支持
        热重载：任一变化必须重启进程，prepare 以稳定错误信息拒绝。
        """
        errors = [
            f"runtime.{name} change requires process restart"
            for name in restart_required_runtime_fields(self._current.runtime, candidate.runtime)
        ]
        if self._current.ads_local != candidate.ads_local:
            errors.append("ADS local identity change requires process restart")
        return errors or None

    async def activate_config(self, revision_id: str) -> ReloadResult:
        """激活已准备配置；仅此阶段执行 CollectorRuntime.reconfigure。"""
        started = time.monotonic()

        async with self._reload_lock:
            if self._prepared_revision != revision_id:
                return ReloadResult(
                    success=False,
                    diff=ConfigDiff(),
                    errors=[
                        "prepared revision mismatch: "
                        f"expected={self._prepared_revision!r} requested={revision_id!r}"
                    ],
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            candidate = self._prepared_config
            diff = self._prepared_diff
            candidate_hash = self._prepared_hash

            if candidate is None or diff is None or candidate_hash is None:
                return ReloadResult(
                    success=False,
                    diff=ConfigDiff(),
                    errors=["no prepared configuration"],
                    duration_ms=(time.monotonic() - started) * 1000,
                )

            if diff.has_any_changes:
                errors = await self._runtime.reconfigure(candidate, diff)
                if errors:
                    return ReloadResult(
                        success=False,
                        diff=diff,
                        errors=errors,
                        duration_ms=(time.monotonic() - started) * 1000,
                    )

            self._current = candidate
            self._config_hash = candidate_hash
            self._active_revision = revision_id
            self._prepared_revision = None
            self._prepared_config = None
            self._prepared_diff = None
            self._prepared_hash = None

        logger.info("Activated config revision=%s", revision_id)
        return ReloadResult(
            success=True,
            diff=diff,
            duration_ms=(time.monotonic() - started) * 1000,
        )

    async def abort_config(self, revision_id: str) -> bool:
        """幂等清理指定 prepared revision，不修改当前 CollectorRuntime。"""
        async with self._reload_lock:
            if self._prepared_revision != revision_id:
                return False
            self._prepared_revision = None
            self._prepared_config = None
            self._prepared_diff = None
            self._prepared_hash = None
            return True
