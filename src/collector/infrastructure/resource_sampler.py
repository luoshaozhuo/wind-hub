"""Periodic process resource observations without synthetic values."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from collector.application.telemetry import CollectorTelemetry


class CollectorResourceSampler:
    """Measure CPU, resident memory and threads on supported Linux hosts."""

    def __init__(self, telemetry: CollectorTelemetry) -> None:
        self._telemetry = telemetry
        self._task: asyncio.Task[None] | None = None
        self._last_cpu = time.process_time()
        self._last_time = time.monotonic()

    def sample(self) -> None:
        now = time.monotonic()
        cpu = time.process_time()
        elapsed = now - self._last_time
        if elapsed > 0:
            self._telemetry.gauge(
                "collector.resource.cpu_percent",
                100.0 * max(0.0, cpu - self._last_cpu) / elapsed,
            )
        self._last_time, self._last_cpu = now, cpu

        statm = Path("/proc/self/statm")
        try:
            fields = statm.read_text(encoding="ascii").split()
            resident_pages = int(fields[1])
            self._telemetry.gauge(
                "collector.resource.rss_bytes",
                float(resident_pages * os.sysconf("SC_PAGE_SIZE")),
            )
        except (OSError, IndexError, ValueError):
            pass

        status = Path("/proc/self/status")
        try:
            for line in status.read_text(encoding="ascii").splitlines():
                if line.startswith("Threads:"):
                    self._telemetry.gauge("collector.resource.threads", float(line.split()[1]))
                    break
        except (OSError, IndexError, ValueError):
            pass

    async def _loop(self) -> None:
        while True:
            self.sample()
            await asyncio.sleep(5)

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="collector-resource-metrics")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
