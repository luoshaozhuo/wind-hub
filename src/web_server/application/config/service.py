"""Web Server 配置事务：基于 Commander CommonService 的正式快照协议。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from core.configuration.digest import config_dir_digest
from web_server.application.config.diff import ConfigSnapshotDiff, diff_config_snapshots
from web_server.application.config.site_loader import load_domain
from web_server.application.config.snapshot import ConfigSnapshot
from web_server.application.port.collector_directory import CollectorDirectory
from web_server.application.port.worker import CommanderPort
from web_server.application.worker.model import COMMANDER_WORKER_ID

logger = logging.getLogger(__name__)
ConfigCommitHook = Callable[[ConfigSnapshot, ConfigSnapshot], None]


@dataclass(frozen=True, slots=True)
class ReloadResult:
    """本地配置与远端 Commander 提交的联合结果。"""

    success: bool
    diff: ConfigSnapshotDiff
    errors: list[str] = field(default_factory=list)
    duration_ms: float = 0.0
    restart_required: bool = False


class ConfigService:
    """一个串行事务所有者；不模拟 Commander 已废弃的配置 RPC。"""

    def __init__(
        self,
        config_dir: str | Path,
        collectors: CollectorDirectory,
        commander: CommanderPort,
        current_config: ConfigSnapshot,
    ) -> None:
        self._config_dir = Path(config_dir).resolve()
        self._commander = commander
        names = collectors.list_worker_ids()
        if not names:
            raise ValueError("at least one collector worker is required")
        if COMMANDER_WORKER_ID in names:
            raise ValueError("collector worker ID conflicts with Commander")
        self._current = current_config
        self._desired_revision: str | None = None
        self._desired_config_hash: str | None = None
        self._transaction_lock = asyncio.Lock()
        self._accept_transactions = True
        self._applied_files = self._snapshot_config_files()
        self._applied_config_hash = config_dir_digest(self._config_dir)
        self._commit_hooks: list[ConfigCommitHook] = []

    def add_commit_hook(self, hook: ConfigCommitHook) -> None:
        self._commit_hooks.append(hook)

    @property
    def config_dir(self) -> Path:
        return self._config_dir

    @property
    def current_config(self) -> ConfigSnapshot:
        return self._current

    @property
    def desired_revision(self) -> str | None:
        return self._desired_revision

    @property
    def desired_config_hash(self) -> str | None:
        return self._desired_config_hash

    @staticmethod
    def load_directory(config_dir: str | Path) -> ConfigSnapshot:
        return load_domain(Path(config_dir))

    def load_disk(self) -> ConfigSnapshot:
        return self.load_directory(self._config_dir)

    def initialize_desired_revision(self) -> None:
        """启动基线仅代表本地配置；不得伪装为 Commander runtime revision。"""
        if self._desired_revision is not None:
            return
        before = config_dir_digest(self._config_dir)
        candidate = self.load_disk()
        after = config_dir_digest(self._config_dir)
        if before != after:
            raise ValueError("config changed while initializing desired revision")
        if diff_config_snapshots(self._current, candidate).has_changes:
            raise ValueError("current config differs from disk at startup")
        self._desired_config_hash = after

    def stop_accepting_transactions(self) -> None:
        self._accept_transactions = False

    async def wait_for_transactions(self, timeout: float) -> bool:
        self.stop_accepting_transactions()
        try:
            async with asyncio.timeout(timeout):
                async with self._transaction_lock:
                    return True
        except TimeoutError:
            return False

    def _snapshot_config_files(self) -> dict[Path, bytes]:
        paths: dict[Path, bytes] = {}
        for path in self._config_dir.rglob("*"):
            if (
                path.is_file()
                and path.suffix.lower() in {".yaml", ".yml"}
                and ".history" not in path.relative_to(self._config_dir).parts
            ):
                paths[path.resolve()] = path.read_bytes()
        return paths

    def _load_candidate(self) -> tuple[ConfigSnapshot, str, dict[Path, bytes]]:
        before = config_dir_digest(self._config_dir)
        candidate = self.load_disk()
        files = self._snapshot_config_files()
        after = config_dir_digest(self._config_dir)
        if before != after:
            raise ValueError("config changed while reading candidate")
        return candidate, after, files

    def _changed_files(
        self, current: dict[Path, bytes], remote: dict[str, bytes]
    ) -> dict[str, bytes]:
        """只接受 Commander 明确拥有的配置路径，绝不猜测默认路径。"""
        removed = set(self._applied_files) - set(current)
        if removed:
            raise ValueError("snapshot RPC does not support deletion: " + str(sorted(removed)))
        changed = {
            path: content
            for path, content in current.items()
            if self._applied_files.get(path) != content
        }
        updates: dict[str, bytes] = {}
        root = self._config_dir.parent
        for path, content in changed.items():
            if not path.is_relative_to(root):
                raise ValueError(f"config outside managed root: {path}")
            relative = path.relative_to(root).as_posix()
            if relative not in remote:
                raise ValueError(
                    f"modified file {relative!r} is not in Commander config_sources; "
                    "a legacy Web configuration cannot be pushed as a shared snapshot"
                )
            updates[relative] = content
        return updates

    def _commit(self, candidate: ConfigSnapshot, revision: str, local_digest: str) -> None:
        previous = self._current
        self._current = candidate
        self._desired_revision = revision
        self._desired_config_hash = local_digest
        self._applied_files = self._snapshot_config_files()
        self._applied_config_hash = local_digest
        for hook in self._commit_hooks:
            try:
                hook(previous, candidate)
            except Exception:
                logger.warning("config commit hook failed", exc_info=True)

    def _failure(self, started: float, diff: ConfigSnapshotDiff, message: str) -> ReloadResult:
        return ReloadResult(
            success=False,
            diff=diff,
            errors=[message],
            duration_ms=(time.monotonic() - started) * 1000,
        )

    async def reload(self, *, force_workers: bool = False) -> ReloadResult:
        if not self._accept_transactions:
            return self._failure(
                time.monotonic(),
                diff_config_snapshots(self._current, self._current),
                "config transactions are shutting down",
            )
        async with self._transaction_lock:
            started = time.monotonic()
            if not self._accept_transactions:
                return self._failure(
                    started,
                    diff_config_snapshots(self._current, self._current),
                    "config transactions are shutting down",
                )
            try:
                candidate, local_digest, local_files = self._load_candidate()
            except Exception as exc:
                return self._failure(
                    started,
                    diff_config_snapshots(self._current, self._current),
                    f"local configuration invalid: {exc}",
                )
            diff = diff_config_snapshots(self._current, candidate)
            if (
                not diff.has_changes
                and not force_workers
                and local_digest == self._applied_config_hash
            ):
                return ReloadResult(
                    success=True,
                    diff=diff,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            try:
                disk = await self._commander.get_config_snapshot("disk")
                if not disk.revision:
                    raise RuntimeError("Commander returned no disk revision")
                files = self._changed_files(local_files, disk.files)
                if diff.has_changes and not files:
                    raise RuntimeError("local semantic changes have no Commander-owned files")
                pushed = await self._commander.push_config(files, disk.revision)
                if not pushed.valid or not pushed.candidate_id:
                    raise RuntimeError("Commander candidate rejected: " + "; ".join(pushed.errors))
                if pushed.base_revision != disk.revision:
                    raise RuntimeError("Commander candidate base revision differs from disk")
                applied = await self._commander.apply_config(pushed.candidate_id, disk.revision)
                if not applied.effective_revision:
                    raise RuntimeError("Commander Apply returned no effective runtime revision")
                if applied.errors:
                    raise RuntimeError(
                        "Commander Apply may have committed files but failed convergence: "
                        + "; ".join(applied.errors)
                    )
                confirmed = await self._commander.get_config_snapshot("disk")
                if not applied.file_commit or applied.file_commit != confirmed.revision:
                    raise RuntimeError("Commander Apply disk commit cannot be confirmed")
                if any(confirmed.files.get(name) != body for name, body in files.items()):
                    raise RuntimeError("Commander disk content differs from pushed candidate")
                if config_dir_digest(self._config_dir) != local_digest:
                    raise RuntimeError(
                        "local configuration changed during remote Apply; "
                        "manual reconciliation required"
                    )
            except Exception as exc:
                # An RPC timeout after Push/Apply is UNKNOWN, never automatically retry Apply.
                return self._failure(
                    started,
                    diff,
                    "Commander snapshot transaction is not confirmed; "
                    f"manual status/reconciliation required: {exc}",
                )
            self._commit(candidate, applied.effective_revision, local_digest)
            return ReloadResult(
                success=True,
                diff=diff,
                duration_ms=(time.monotonic() - started) * 1000,
                restart_required=applied.restart_required,
            )

    async def reconcile_workers(self) -> dict[str, str]:
        """只检查远端状态；未知提交绝不自动重新 Apply。"""
        if not self._accept_transactions:
            return {COMMANDER_WORKER_ID: "transactions-closed"}
        async with self._transaction_lock:
            try:
                status = await self._commander.status()
            except Exception as exc:
                return {COMMANDER_WORKER_ID: "status-error:" + str(exc)}
            if not status.running:
                return {COMMANDER_WORKER_ID: "not-running"}
            if not self._desired_revision:
                return {COMMANDER_WORKER_ID: "no-confirmed-remote-revision"}
            if status.active_revision != self._desired_revision:
                return {COMMANDER_WORKER_ID: "revision-mismatch"}
            return {COMMANDER_WORKER_ID: "already-current"}


__all__ = ["ConfigService", "ReloadResult"]
