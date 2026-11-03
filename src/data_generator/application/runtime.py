"""Single active generation lifecycle and bounded model/replay scheduling."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from data_generator.domain.scenario import Scenario, Settings, resolve_history
from data_generator.application.models import generate_values
from data_generator.application.replay import iter_records, replay_deadline
from data_generator.domain.model import DataBatch, DataPoint, GeneratorError, Scalar, validate_scalar
from data_generator.ports.output import OutputPort


@dataclass(slots=True)
class GenerationState:
    generation_id: str
    scenario: Scenario
    actual_state: str = "STARTING"
    desired_state: str = "RUNNING"
    last_error: str = ""
    started_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: str
    timestamp: datetime
    severity: str
    source: str
    error_code: str
    message: str


class Runtime:
    def __init__(self, settings: Settings, writer: OutputPort) -> None:
        self.settings = settings
        self.writer = writer
        self.generations: dict[str, GenerationState] = {}
        self.events: deque[RuntimeEvent] = deque(maxlen=1000)
        self._active: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.counters: dict[str, int] = {
            "generated": 0, "batches": 0, "replay_read": 0,
            "attempted": 0, "confirmed": 0, "failed": 0,
            "unknown": 0, "skipped_steps": 0,
        }

    def record(self, severity: str, source: str, error_code: str, message: str) -> None:
        self.events.append(RuntimeEvent(str(uuid.uuid4()), datetime.now(timezone.utc),
                                        severity, source, error_code, message))

    def _unsafe_for(self, generation: GenerationState) -> bool:
        unsafe = self.writer.unsafe_gateways
        return any(mapping.target.gateway_id in unsafe
                   for mapping in generation.scenario.mappings)

    async def commit_configuration(
        self, commit: Callable[[], tuple[Settings, bool]],
    ) -> tuple[Settings, bool]:
        """Keep StartGeneration and configuration disk/active switch serialized."""
        async with self._lock:
            changed, restart_required = await asyncio.to_thread(commit)
            previous = self.settings
            # Service config (including its source paths) is restart-only.
            # Never mix new scenario/point bindings into the old service epoch.
            self.settings = previous if restart_required else changed
            return changed, restart_required

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
            self.record("INFO", generation.generation_id, "START", scenario_id)
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
        await self.writer.close()

    async def _run(self, generation: GenerationState) -> None:
        generation.actual_state = "RUNNING"
        try:
            if generation.scenario.source["kind"] == "simulation":
                await self._simulate(generation)
            else:
                await self._replay(generation)
            generation.actual_state = "COMPLETED"
            self.record("INFO", generation.generation_id, "COMPLETED",
                        "generation finished")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            generation.last_error = str(exc)
            generation.actual_state = "FAILED"
            self.record("ERROR", generation.generation_id, "FAILED", str(exc))
        finally:
            await self.writer.drain()
            async with self._lock:
                if self._unsafe_for(generation):
                    generation.actual_state = "STOPPING"
                    generation.desired_state = "STOPPED"
                    generation.last_error = "UNSETTLED_GATEWAY_OUTPUT"
                elif self._active == generation.generation_id:
                    self._active = None

    async def _simulate(self, generation: GenerationState) -> None:
        scenario = generation.scenario
        interval = float(scenario.source["interval_seconds"])
        if interval <= 0:
            raise GeneratorError("invalid simulation interval")
        seed = int(scenario.source.get("seed", 0))
        seq = 0
        epoch = time.monotonic()
        while generation.desired_state == "RUNNING":
            now = time.monotonic()
            next_step = max(seq + 1, math.floor((now - epoch) / interval) + 1)
            if next_step > seq + 1:
                skipped = next_step - seq - 1
                self.counters["skipped_steps"] += skipped
                self.record("WARNING", generation.generation_id, "SKIPPED_PERIOD",
                            str(skipped))
            seq = next_step
            await asyncio.sleep(max(0.0, epoch + seq * interval - now))
            stamp = datetime.now(timezone.utc)
            values = generate_values(seed, seq, scenario.points)
            await self._emit(generation, seq, stamp, values)

    async def _replay(self, generation: GenerationState) -> None:
        scenario = generation.scenario
        source = scenario.source
        start = datetime.fromisoformat(source["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(source["end_time"].replace("Z", "+00:00"))
        speed = float(source["speed"])
        replay_config = self.settings.service.get("replay", {})
        seq = 0
        first_time: datetime | None = None
        origin = 0.0
        async for record in iter_records(
            resolve_history(self.settings.root, source["file"]), start, end,
            read_batch_size=int(replay_config.get("read_batch_size", 1000)),
            max_file_bytes=int(replay_config.get("max_file_bytes", 268435456)),
            max_records=int(replay_config.get("max_records", 1000000)),
        ):
            if generation.desired_state != "RUNNING":
                return
            seq += 1
            self.counters["replay_read"] += 1
            if first_time is None:
                first_time = record.source_time
                origin = time.monotonic()
            deadline = replay_deadline(first_time, record.source_time, origin, speed)
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            key = (record.device_id, record.point_id)
            if key not in scenario.points:
                raise GeneratorError(f"CSV references unknown point: {key}")
            point = scenario.points[key]
            kind = point["data_type"]
            if kind == "bool":
                if record.value.lower() not in ("true", "false"):
                    raise GeneratorError("invalid CSV bool")
                value = record.value.lower() == "true"
            elif kind.startswith(("int", "uint")):
                value = int(record.value)
            elif kind.startswith("float"):
                value = float(record.value)
            else:
                value = record.value
            await self._emit(
                generation, seq, record.source_time,
                {key: validate_scalar(value, kind)},
            )

    async def _emit(self, generation: GenerationState, sequence: int,
                    timestamp: datetime, values: dict[tuple[str, str], Scalar]) -> None:
        batch = DataBatch(generation.generation_id, sequence, datetime.now(timezone.utc),
                          tuple(DataPoint(device, point, value, timestamp)
                                for (device, point), value in values.items()))
        self.counters["generated"] += len(batch.points)
        self.counters["batches"] += 1
        outcomes = await self.writer.send(generation.scenario, batch)
        self.counters["attempted"] += len(outcomes)
        for result in outcomes:
            if result.state == "SUCCESS":
                self.counters["confirmed"] += 1
            elif result.state == "FAILED":
                self.counters["failed"] += 1
            else:
                self.counters["unknown"] += 1
                self.record("WARNING", generation.generation_id, "UNKNOWN_OUTPUT", result.error_code)
