"""Single active generation lifecycle and bounded model/replay scheduling."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from data_generator.application.config import Scenario, Settings, resolve_history
from data_generator.application.models import random_value, static_value
from data_generator.application.replay import load_records, replay_deadline
from data_generator.domain.model import DataBatch, DataPoint, GeneratorError, validate_scalar
from data_generator.infrastructure.gateway import GatewayWriter


@dataclass(slots=True)
class GenerationState:
    generation_id: str
    scenario: Scenario
    actual_state: str = "STARTING"
    desired_state: str = "RUNNING"
    last_error: str = ""
    started_at: datetime | None = None


class Runtime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        output = settings.service.get("output", {})
        buffer = settings.service.get("buffer", {})
        self.writer = GatewayWriter(float(output.get("grpc_timeout_seconds", 3.0)),
                                    int(buffer.get("max_points_per_batch", 1000)))
        self.generations: dict[str, GenerationState] = {}
        self._active: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.counters: dict[str, int] = {"generated": 0, "confirmed": 0,
                                        "failed": 0, "unknown": 0}

    async def start(self, scenario_id: str, version: str | None,
                    revision: str) -> GenerationState:
        async with self._lock:
            if revision and revision != self.settings.revision:
                raise GeneratorError("expected_revision mismatch")
            if self._active is not None:
                raise GeneratorError("active generation already exists")
            scenario = self.settings.scenarios.get(scenario_id)
            if scenario is None:
                raise GeneratorError("scenario not found")
            if version is not None and version != scenario.version:
                raise GeneratorError("scenario version unavailable")
            generation = GenerationState(str(uuid.uuid4()), scenario,
                                         started_at=datetime.now(timezone.utc))
            self.generations[generation.generation_id] = generation
            self._active = generation.generation_id
            self._task = asyncio.create_task(self._run(generation))
            return generation

    async def stop(self, generation_id: str, revision: str) -> GenerationState:
        async with self._lock:
            if revision and revision != self.settings.revision:
                raise GeneratorError("expected_revision mismatch")
            generation = self.generations.get(generation_id)
            if generation is None:
                raise GeneratorError("generation not found")
            generation.desired_state = "STOPPED"
            if self._active != generation_id or self._task is None:
                return generation
            task = self._task
            generation.actual_state = "STOPPING"
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await self.writer.drain()
        async with self._lock:
            generation.actual_state = "STOPPED"
            if self._active == generation_id:
                self._active = None
        return generation

    async def shutdown(self) -> None:
        if self._active is not None:
            await self.stop(self._active, "")
        else:
            await self.writer.drain()

    async def _run(self, generation: GenerationState) -> None:
        generation.actual_state = "RUNNING"
        try:
            if generation.scenario.source["kind"] == "simulation":
                await self._simulate(generation)
            else:
                await self._replay(generation)
            generation.actual_state = "COMPLETED"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            generation.last_error = str(exc)
            generation.actual_state = "FAILED"
        finally:
            # A completed task only releases ownership once outstanding writes settle.
            await self.writer.drain()
            async with self._lock:
                if self._active == generation.generation_id:
                    self._active = None

    async def _simulate(self, generation: GenerationState) -> None:
        scenario = generation.scenario
        interval = float(scenario.source["interval_seconds"])
        if interval <= 0:
            raise GeneratorError("invalid simulation interval")
        seed = int(scenario.source.get("seed", 0))
        seq = 0
        while generation.desired_state == "RUNNING":
            seq += 1
            stamp = datetime.now(timezone.utc)
            values = {}
            for (device_id, point_id), point in scenario.points.items():
                model = point.get("model", {})
                kind = model.get("kind")
                if kind == "random":
                    value = random_value(seed, device_id, point_id, seq,
                                         {**model, "data_type": point.get("data_type", "float64")})
                elif kind == "static":
                    value = static_value({**model, "data_type": point.get("data_type", "float64")},
                                         {key[1]: val for key, val in values.items()
                                          if key[0] == device_id})
                else:
                    raise GeneratorError(f"unsupported model: {kind}")
                values[(device_id, point_id)] = validate_scalar(value, point["data_type"])
            await self._emit(generation, seq, stamp, values)
            await asyncio.sleep(interval)

    async def _replay(self, generation: GenerationState) -> None:
        scenario = generation.scenario
        source = scenario.source
        start = datetime.fromisoformat(source["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(source["end_time"].replace("Z", "+00:00"))
        records = load_records(resolve_history(self.settings.root, source["file"]), start, end)
        if not records:
            return
        speed = float(source["speed"])
        origin = time.monotonic()
        for index, record in enumerate(records, 1):
            deadline = replay_deadline(records[0].source_time, record.source_time,
                                       origin, speed)
            await asyncio.sleep(max(0, deadline-time.monotonic()))
            key = (record.device_id, record.point_id)
            point = scenario.points[key]
            kind = point["data_type"]
            if kind == "bool":
                value = record.value.lower() == "true"
            elif kind.startswith(("int", "uint")):
                value = int(record.value)
            elif kind.startswith("float"):
                value = float(record.value)
            else:
                value = record.value
            await self._emit(generation, index, record.source_time,
                             {key: validate_scalar(value, kind)})

    async def _emit(self, generation: GenerationState, sequence: int,
                    timestamp: datetime, values: dict[tuple[str, str], object]) -> None:
        batch = DataBatch(generation.generation_id, sequence, datetime.now(timezone.utc),
                          tuple(DataPoint(device, point, value, timestamp)
                                for (device, point), value in values.items()))
        self.counters["generated"] += len(batch.points)
        outcomes = await self.writer.send(generation.scenario, batch)
        for result in outcomes:
            if result.state == "SUCCESS":
                self.counters["confirmed"] += 1
            elif result.state == "FAILED":
                self.counters["failed"] += 1
            else:
                self.counters["unknown"] += 1
