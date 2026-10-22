"""统一仿真时钟；命令仅在固定仿真步入口提交。"""
from __future__ import annotations

import time

from seahorse.application.command_queue import CommandQueue
from seahorse.application.wind_farm_service import WindFarmService
from seahorse.domain.models import FarmSnapshot, SimulationTime


class SeahorseRuntime:
    def __init__(
        self, service: WindFarmService, step_seconds: float, commands: CommandQueue | None = None
    ) -> None:
        if not 0 < step_seconds <= 60:
            raise ValueError("step_seconds must be in (0, 60]")
        self._service = service
        self._step_seconds = step_seconds
        self._commands = commands
        self._index = 0
        self._running = False

    def step(self) -> FarmSnapshot:
        if self._commands is not None:
            for command in self._commands.drain():
                self._service.command(command)
        next_index = self._index + 1
        snapshot = self._service.step(
            SimulationTime(next_index, next_index * self._step_seconds, self._step_seconds)
        )
        self._index = next_index
        return snapshot

    def run(self, steps: int | None = None) -> None:
        if steps is not None and steps < 0:
            raise ValueError("steps cannot be negative")
        if self._running:
            raise RuntimeError("runtime is already running")
        self._running = True
        count = 0
        try:
            while self._running and (steps is None or count < steps):
                start = time.monotonic()
                self.step()
                count += 1
                remaining = self._step_seconds - (time.monotonic() - start)
                if remaining > 0 and (steps is None or count < steps):
                    time.sleep(remaining)
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False
