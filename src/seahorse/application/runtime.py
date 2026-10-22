"""固定步仿真；每条命令独立完成或失败，不中断后续命令。"""
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
        self._failed = False

    def step(self) -> FarmSnapshot:
        if self._failed:
            raise RuntimeError("simulation stopped after a failed step")
        if self._commands is not None:
            for item in self._commands.drain():
                try:
                    self._service.command(item.command)
                except (ValueError, TypeError, KeyError) as exc:
                    item.outcome.set_exception(exc)
                else:
                    item.outcome.set_result(None)
        next_index = self._index + 1
        try:
            snapshot = self._service.step(
                SimulationTime(next_index, next_index * self._step_seconds, self._step_seconds)
            )
        except Exception:
            # 模型和 Modbus 发布不可回滚，拒绝重复推进同一时间步。
            self._failed = True
            if self._commands is not None:
                self._commands.close()
            raise
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
            if self._commands is not None:
                self._commands.close()

    def stop(self) -> None:
        self._running = False
