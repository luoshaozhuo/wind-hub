"""Config service — configuration loading, diff computation, and hot-reload."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from wind_hub.config.loader import load_config
from wind_hub.config.routing import RoutingTable
from wind_hub.config.schema import Config, DeviceConfig, PointConfig, SinkConfig
from wind_hub.domain.engine.pipeline import Pipeline
from wind_hub.domain.engine.router import Router
from wind_hub.domain.engine.scheduler import Scheduler
from wind_hub.domain.model.reload import ConfigDiff, DeviceDiff, ReloadResult, SinkDiff
from wind_hub.domain.port.inbound import ConfigUseCase
from wind_hub.domain.port.outbound import ProcessorPort, ProtocolPort, SinkPort

logger = logging.getLogger(__name__)


def compute_diff(old: Config, new: Config) -> ConfigDiff:
    """Compute the difference between two Config snapshots.

    Comparison rules:

    - **Devices**: keyed by ``device_id``.  A device is "updated" when
      any field of its ``DeviceConfig`` differs (deep equality via
      ``model_dump()``).
    - **Sinks**: keyed by ``sink.name``, same logic.
    - **Points**: list-level deep comparison — any difference sets
      ``points_changed=True``.
    - **Rules**: list-level deep comparison — any difference sets
      ``rules_changed=True``.
    - **Pipeline**: processor list comparison.

    This is pure logic — no IO, no side-effects.
    """
    # -- devices -----------------------------------------------------------
    old_dev_ids = {d.device_id: d for d in old.devices.devices}
    new_dev_ids = {d.device_id: d for d in new.devices.devices}

    old_set = set(old_dev_ids)
    new_set = set(new_dev_ids)

    devices = DeviceDiff(
        added=sorted(new_set - old_set),
        removed=sorted(old_set - new_set),
    )

    updated: list[str] = []
    unchanged: list[str] = []
    for did in old_set & new_set:
        if old_dev_ids[did].model_dump() != new_dev_ids[did].model_dump():
            updated.append(did)
        else:
            unchanged.append(did)
    devices.updated = sorted(updated)
    devices.unchanged = sorted(unchanged)

    # -- sinks -------------------------------------------------------------
    old_sinks = {s.name: s for s in old.system.sinks}
    new_sinks = {s.name: s for s in new.system.sinks}

    old_sink_set = set(old_sinks)
    new_sink_set = set(new_sinks)

    sinks = SinkDiff(
        added=sorted(new_sink_set - old_sink_set),
        removed=sorted(old_sink_set - new_sink_set),
    )

    sink_updated: list[str] = []
    sink_unchanged: list[str] = []
    for name in old_sink_set & new_sink_set:
        if old_sinks[name].model_dump() != new_sinks[name].model_dump():
            sink_updated.append(name)
        else:
            sink_unchanged.append(name)
    sinks.updated = sorted(sink_updated)
    sinks.unchanged = sorted(sink_unchanged)

    # -- points / rules / pipeline -----------------------------------------
    points_changed = _items_changed(old.points.points, new.points.points)
    rules_changed = _items_changed(old.routing.rules, new.routing.rules)
    pipeline_changed = old.system.pipeline.processors != new.system.pipeline.processors

    return ConfigDiff(
        devices=devices,
        sinks=sinks,
        points_changed=points_changed,
        rules_changed=rules_changed,
        pipeline_changed=pipeline_changed,
    )


def _items_changed(old_items: Sequence[Any], new_items: Sequence[Any]) -> bool:
    """True when two lists of pydantic models differ."""
    if len(old_items) != len(new_items):
        return True
    return any(a.model_dump() != b.model_dump() for a, b in zip(old_items, new_items, strict=True))


class ConfigService(ConfigUseCase):
    """Configuration service — hot-reload orchestration.

    实现 :class:`~wind_hub.domain.port.inbound.ConfigUseCase`：
    ``reload()`` 编排热重载，``current_config`` 暴露当前配置。

    Holds the current ``Config`` and a reference to the running
    ``Scheduler``.  On ``reload()`` it:

    1. Loads new configuration files from disk.
    2. Computes a ``ConfigDiff``.
    3. Applies changes to the scheduler:
       - Add / remove / rebuild devices.
       - Add / remove / rebuild sinks.
       - Replace the routing table (if points or rules changed).
       - Replace the pipeline (if the processor list changed).
    4. Returns a ``ReloadResult``.

    If loading fails, no changes are applied and ``ReloadResult.success``
    is ``False``.
    """

    def __init__(
        self,
        config_dir: str | Path,
        scheduler: Scheduler,
        protocol_factory: Callable[[DeviceConfig], ProtocolPort],
        sink_factory: Callable[[SinkConfig], SinkPort],
        processor_factory: Callable[[str, list[PointConfig]], ProcessorPort] | None = None,
    ) -> None:
        self._config_dir = Path(config_dir)
        self._scheduler = scheduler
        self._protocol_factory = protocol_factory
        self._sink_factory = sink_factory
        self._processor_factory = processor_factory

        # Load initial config
        self._current = load_config(self._config_dir)

    @property
    def current_config(self) -> Config:
        """The currently active configuration."""
        return self._current

    async def reload(self) -> ReloadResult:
        """Run a full hot-reload cycle.

        Returns a ``ReloadResult`` describing what changed and whether
        the operation succeeded.
        """
        t0 = time.monotonic()
        errors: list[str] = []

        # 1. Load new config — bail early on failure
        try:
            new_cfg = load_config(self._config_dir)
        except Exception as exc:
            logger.error("Reload aborted — config load failed: %s", exc)
            return ReloadResult(
                success=False,
                diff=ConfigDiff(),
                errors=[str(exc)],
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        # 2. Compute diff
        diff = compute_diff(self._current, new_cfg)
        if not diff.has_any_changes:
            logger.info("Reload: no changes detected")
            return ReloadResult(
                success=True,
                diff=diff,
                duration_ms=(time.monotonic() - t0) * 1000,
            )

        # 3. Apply device changes
        try:
            await self._apply_device_diff(diff, new_cfg)
        except Exception as exc:
            logger.error("Device diff apply failed: %s", exc, exc_info=True)
            errors.append(f"device: {exc}")

        # 4. Apply sink changes
        try:
            await self._apply_sink_diff(diff, new_cfg)
        except Exception as exc:
            logger.error("Sink diff apply failed: %s", exc, exc_info=True)
            errors.append(f"sink: {exc}")

        # 5. Rebuild routing table
        if diff.points_changed or diff.rules_changed:
            try:
                table = RoutingTable(
                    new_cfg.routing.rules,
                    new_cfg.points.points,
                    new_cfg.routing.unmatched_policy,
                )
                await self._scheduler.replace_router(Router(table))
                logger.info("Routing table rebuilt (%d entries)", table.size)
            except Exception as exc:
                logger.error("Routing table rebuild failed: %s", exc, exc_info=True)
                errors.append(f"routing: {exc}")

        # 6. Rebuild pipeline（点表变更也会让 Processor 重新注入新点表，死区状态重置可接受）
        if (diff.pipeline_changed or diff.points_changed) and self._processor_factory is not None:
            try:
                processors = [
                    self._processor_factory(name, new_cfg.points.points)
                    for name in new_cfg.system.pipeline.processors
                ]
                await self._scheduler.replace_pipeline(Pipeline(processors))
                logger.info("Pipeline rebuilt (%d processors)", len(processors))
            except Exception as exc:
                logger.error("Pipeline rebuild failed: %s", exc, exc_info=True)
                errors.append(f"pipeline: {exc}")

        # 7. Commit new config
        self._current = new_cfg

        duration_ms = (time.monotonic() - t0) * 1000
        success = len(errors) == 0
        logger.info(
            "Reload %s in %.1f ms",
            "succeeded" if success else "partially failed",
            duration_ms,
        )
        return ReloadResult(
            success=success,
            diff=diff,
            errors=errors,
            duration_ms=duration_ms,
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    async def _apply_device_diff(self, diff: ConfigDiff, new_cfg: Config) -> None:
        new_devices = {d.device_id: d for d in new_cfg.devices.devices}

        for did in diff.devices.removed:
            await self._scheduler.remove_device(did)

        for did in diff.devices.added:
            cfg = new_devices[did]
            protocol = self._protocol_factory(cfg)
            await self._scheduler.add_device(
                did, cfg, protocol, self._points_for_device(new_cfg, did)
            )

        for did in diff.devices.updated:
            cfg = new_devices[did]
            protocol = self._protocol_factory(cfg)
            await self._scheduler.rebuild_device(
                did, cfg, protocol, self._points_for_device(new_cfg, did)
            )

    def _points_for_device(self, config: Config, device_id: str) -> list[PointConfig]:
        """取某设备的完整点表（按 device_id 过滤）。"""
        return [p for p in config.points.points if p.device_id == device_id]

    async def _apply_sink_diff(self, diff: ConfigDiff, new_cfg: Config) -> None:
        new_sinks = {s.name: s for s in new_cfg.system.sinks}

        for name in diff.sinks.removed:
            await self._scheduler.remove_sink(name)

        for name in diff.sinks.added:
            cfg = new_sinks[name]
            sink = self._sink_factory(cfg)
            await self._scheduler.add_sink(name, cfg, sink)

        for name in diff.sinks.updated:
            cfg = new_sinks[name]
            sink = self._sink_factory(cfg)
            await self._scheduler.rebuild_sink(name, cfg, sink)
