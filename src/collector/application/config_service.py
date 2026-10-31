"""Collector application-level configuration activation.

The service validates a complete candidate and applies only affected runtime
resources. File transaction ownership belongs to infrastructure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from .config import CollectorConfig
from .latest_values import LatestValues
from .reload import (
    ConfigDiff,
    ReloadResult,
    compute_diff,
    restart_required_runtime_fields,
)
from .runtime import CollectorRuntime

logger = logging.getLogger(__name__)

ConfigLoader = Callable[[], CollectorConfig]
ConfigDigest = Callable[[], str]


class CollectorConfigService:
    """Apply validated effective configuration without a two-phase protocol."""

    def __init__(
        self,
        runtime: CollectorRuntime,
        current_config: CollectorConfig,
        *,
        load_config: ConfigLoader,
        config_digest: ConfigDigest,
        config_hash: str,
        latest_values: LatestValues | None = None,
    ) -> None:
        self._runtime = runtime
        self._current = current_config
        self._latest_values = latest_values
        self._load_config = load_config
        self._config_digest = config_digest
        self._config_hash = config_hash
        self._active_revision = config_hash
        self._reload_lock = asyncio.Lock()

    @property
    def current_config(self) -> CollectorConfig:
        return self._current

    @property
    def config_hash(self) -> str:
        return self._config_hash

    @property
    def active_revision(self) -> str:
        return self._active_revision

    def active_snapshot(self) -> tuple[CollectorConfig, str, str]:
        """Read one committed effective configuration snapshot."""
        return self._current, self._active_revision, self._config_hash

    def _restart_required(self, candidate: CollectorConfig) -> list[str]:
        errors = [
            f"runtime.{field} change requires process restart"
            for field in restart_required_runtime_fields(self._current.runtime, candidate.runtime)
        ]
        if self._current.messaging != candidate.messaging:
            errors.append("messaging changes require process restart")
        if self._current.ads_local != candidate.ads_local:
            errors.append("ADS local identity changes require process restart")
        if self._current.units != candidate.units:
            errors.append("unit catalog changes require process restart")
        return errors

    async def reload(self) -> ReloadResult:
        """Validate and converge one candidate, preserving active state on failure."""
        started = time.monotonic()
        async with self._reload_lock:
            diff = ConfigDiff()
            try:
                initial_hash = self._config_digest()
                candidate = self._load_config()
                candidate_hash = self._config_digest()
                if initial_hash != candidate_hash:
                    raise ValueError("configuration changed while loading candidate")
                diff = compute_diff(self._current, candidate)
                restart_required = self._restart_required(candidate)
                if restart_required:
                    return ReloadResult(
                        success=False,
                        diff=diff,
                        errors=restart_required,
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                errors = (
                    await self._runtime.reconfigure(candidate, diff) if diff.has_any_changes else []
                )
                if errors:
                    return ReloadResult(
                        success=False,
                        diff=diff,
                        errors=errors,
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                self._current = candidate
                self._config_hash = candidate_hash
                self._active_revision = candidate_hash
                if self._latest_values is not None:
                    self._latest_values.retain(
                        {
                            str(device_id): frozenset(
                                candidate.point_table_for_device(device_id).points
                            )
                            for device_id in candidate.devices
                        }
                    )
                return ReloadResult(
                    success=True,
                    diff=diff,
                    duration_ms=(time.monotonic() - started) * 1000,
                )
            except Exception as exc:
                logger.exception("Collector configuration reload failed")
                return ReloadResult(
                    success=False,
                    diff=diff,
                    errors=[str(exc) or type(exc).__name__],
                    duration_ms=(time.monotonic() - started) * 1000,
                )
