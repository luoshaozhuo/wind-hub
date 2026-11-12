"""Single active Generation with immutable source binding and safe I/O ownership."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from data_generator.application.model.inputs import (
    WEATHER_INPUTS,
    validate_input,
    validate_weather_input,
    writable_inputs,
)
from data_generator.application.model.random import generate as generate_configured_random
from data_generator.application.models import generate_values
from data_generator.application.physical_engine import PhysicalEngine
from data_generator.application.port.output import OutputPort
from data_generator.application.replay import decode_value, iter_records, replay_deadline
from data_generator.domain.model import (
    DataBatch,
    DataPoint,
    DeliveryResult,
    GeneratorError,
    OutputTarget,
    Scalar,
)
from data_generator.domain.scenario import Scenario, Settings, resolve_history

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class GenerationState:
    generation_id: str
    scenario: Scenario
    bound_settings: Settings
    actual_state: str = "STARTING"
    desired_state: str = "RUNNING"
    last_error: str = ""
    started_at: datetime | None = None
    terminal_state: str | None = None
    physical_engine: PhysicalEngine | None = None
    current_step: int = 0
    pending_inputs: list[tuple[int, str, dict[str, float | int]]] = field(default_factory=list)
    request_history: dict[tuple[str, str], tuple[bytes, int]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    event_id: str
    timestamp: datetime
    severity: str
    source: str
    error_code: str
    message: str


class Runtime:
    """State transitions and ownership changes share one serialized control lock."""

    def __init__(self, settings: Settings, writer: OutputPort) -> None:
        self.settings = settings
        self.writer = writer
        self.generations: dict[str, GenerationState] = {}
        self.events: deque[RuntimeEvent] = deque(maxlen=1000)
        self._active: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.last_targets: dict[OutputTarget, DeliveryResult] = {}
        self.target_counts: dict[OutputTarget, dict[str, int]] = {}
        self.gateway_counts: dict[str, dict[str, int]] = defaultdict(
            lambda: {"SUCCESS": 0, "FAILED": 0, "UNKNOWN": 0}
        )
        self.last_batch_status = "NONE"
        self.observations: deque[tuple[float, str, float]] = deque(maxlen=4096)
        self.latest_replay_timestamp: datetime | None = None
        self.replay_speed: float | None = None
        self.counters: dict[str, int] = {
            "generated": 0,
            "batches": 0,
            "replay_read": 0,
            "attempted": 0,
            "confirmed": 0,
            "failed": 0,
            "unknown": 0,
            "partial_batches": 0,
            "unknown_batches": 0,
            "skipped_steps": 0,
        }

    def record(self, severity: str, source: str, error_code: str, message: str) -> None:
        self.events.append(
            RuntimeEvent(
                str(uuid.uuid4()),
                datetime.now(UTC),
                severity,
                source,
                error_code,
                message,
            )
        )
        logger.log(
            logging.ERROR
            if severity == "ERROR"
            else logging.WARNING
            if severity == "WARNING"
            else logging.INFO,
            "%s: %s",
            error_code,
            message,
            extra={"source_id": source, "stage": error_code},
        )

    @staticmethod
    def _gateway_keys(generation: GenerationState) -> frozenset[str]:
        return frozenset(mapping.target.gateway_id for mapping in generation.scenario.mappings)

    def _blocked(self, generation: GenerationState) -> bool:
        keys = self._gateway_keys(generation)
        return self.writer.has_claims(generation.generation_id) or bool(
            keys & (self.writer.unsafe_gateways | self.writer.unsettled_gateways)
        )

    def _reconcile_locked(self) -> None:
        """Release all reserved keys only after safe source and native I/O settlement."""
        if self._active is None or self._task is None or not self._task.done():
            return
        generation = self.generations[self._active]
        if self._blocked(generation):
            # A failed source remains FAILED while its Gateway ownership stays reserved.
            generation.actual_state = (
                "FAILED" if generation.terminal_state == "FAILED" else "STOPPING"
            )
            if not generation.last_error:
                generation.last_error = "UNSETTLED_GATEWAY_OUTPUT"
            return
        if generation.desired_state == "STOPPED":
            generation.actual_state = "STOPPED"
        elif generation.terminal_state is not None:
            generation.actual_state = generation.terminal_state
        self._active = None
        self._task = None

    async def commit_configuration(
        self,
        commit: Callable[[], tuple[Settings, bool]],
    ) -> tuple[Settings, bool]:
        """Hold the same boundary as Start/Stop while switching active config."""
        async with self._lock:
            changed, restart_required = await asyncio.to_thread(commit)
            if not restart_required:
                self.settings = changed
            self._reconcile_locked()
            return changed, restart_required

    async def get_generation(self, generation_id: str) -> GenerationState | None:
        """Reconcile settled ownership before reporting the authoritative state."""
        async with self._lock:
            self._reconcile_locked()
            return self.generations.get(generation_id)

    async def update_inputs(
        self,
        generation_id: str,
        device_id: str,
        caller: str,
        request_id: str,
        fingerprint: bytes,
        values: dict[str, Scalar],
    ) -> int:
        """Validate the entire batch and reserve exactly one future step."""
        async with self._lock:
            state = self.generations.get(generation_id)
            if (
                state is None
                or self._active != generation_id
                or state.desired_state != "RUNNING"
                or state.actual_state != "RUNNING"
                or state.physical_engine is None
            ):
                raise GeneratorError("generation is not active for physical input control")
            engine = state.physical_engine
            if device_id not in state.scenario.devices or device_id not in engine.models:
                raise GeneratorError("physical device not found in Generation")
            history_key = (caller, request_id)
            if history_key in state.request_history:
                previous, step = state.request_history[history_key]
                if previous != fingerprint:
                    raise GeneratorError("request_id already used with different input")
                return step
            if len(state.request_history) >= 10000:
                raise GeneratorError("generation request-id capacity exceeded")
            kind = engine.kinds[device_id]
            specs = writable_inputs(kind)
            weather_id = engine.weather_refs.get(device_id)
            verified: dict[str, float | int] = {}
            for point_id, value in values.items():
                if point_id in specs:
                    if engine.controller_modes[device_id] == "ppc" and point_id in {
                        "active_power_setpoint",
                        "reactive_power_setpoint",
                    }:
                        raise GeneratorError("PPC owns this device setpoint")
                    verified[point_id] = validate_input(value, specs[point_id])
                elif point_id in WEATHER_INPUTS and weather_id is not None:
                    verified[point_id] = validate_weather_input(value, WEATHER_INPUTS[point_id])
                else:
                    raise GeneratorError(f"unknown or non-writable model input: {point_id}")
            if not verified:
                raise GeneratorError("input batch must not be empty")
            effective_step = state.current_step + 1
            state.pending_inputs.append((effective_step, device_id, verified))
            state.request_history[history_key] = (fingerprint, effective_step)
            return effective_step

    def _install_inputs(self, generation: GenerationState, step: int) -> None:
        """Called on the event loop before advancing each discrete physical step."""
        engine = generation.physical_engine
        if engine is None:
            return
        due = [item for item in generation.pending_inputs if item[0] <= step]
        generation.pending_inputs = [item for item in generation.pending_inputs if item[0] > step]
        for _, device, values in due:
            weather_id = engine.weather_refs.get(device)
            external = {key: float(value) for key, value in values.items() if key in WEATHER_INPUTS}
            model = {
                key: float(value) for key, value in values.items() if key not in WEATHER_INPUTS
            }
            if external:
                if weather_id is None:
                    raise GeneratorError("weather input has no bound weather reference")
                engine.set_weather_inputs(weather_id, external)
            if model:
                engine.set_inputs(device, model)
        generation.current_step = step

    async def start(
        self,
        scenario_id: str,
        version: str | None,
        revision: str,
    ) -> GenerationState:
        async with self._lock:
            if revision and revision != self.settings.revision:
                raise GeneratorError("expected_revision mismatch")
            self._reconcile_locked()
            if self._active is not None:
                active = self.generations[self._active]
                raise GeneratorError(
                    f"target ownership retained by Generation {active.generation_id}"
                )
            scenario = self.settings.scenarios.get(scenario_id)
            if scenario is None:
                raise GeneratorError("scenario not found")
            if version is not None and version != scenario.version:
                raise GeneratorError("scenario version unavailable")
            devices = set(scenario.devices)
            targets = [item.target for item in scenario.mappings]
            if not devices or not targets or len(set(targets)) != len(targets):
                raise GeneratorError("invalid or conflicting source target set")
            unsafe = set(scenario.gateways) & (
                self.writer.unsafe_gateways | self.writer.unsettled_gateways
            )
            if unsafe:
                raise GeneratorError(f"unsettled Gateway targets: {sorted(unsafe)}")
            await self.writer.validate(scenario)
            state = GenerationState(
                str(uuid.uuid4()),
                scenario,
                self.settings,
                started_at=datetime.now(UTC),
            )
            await self.writer.claim(scenario, state.generation_id)
            self.generations[state.generation_id] = state
            self._active = state.generation_id
            self._task = asyncio.create_task(self._run(state))
            self._task.add_done_callback(
                lambda _: asyncio.create_task(self._finish_when_settled(state))
            )
            self.record("INFO", state.generation_id, "STARTED", scenario_id)
            return state

    async def _finish_when_settled(self, generation: GenerationState) -> None:
        """Reap completed source ownership when native I/O becomes trustworthy."""
        timeout = (
            float(
                generation.bound_settings.service.get(
                    "output",
                    {},
                ).get("grpc_timeout_seconds", 3.0)
            )
            * 2
        )
        try:
            safe = await self.writer.wait_settled(self._gateway_keys(generation), timeout)
            if safe:
                await self.writer.release(generation.scenario, generation.generation_id)
        except (OSError, RuntimeError, ValueError) as exc:
            self.record(
                "WARNING",
                generation.generation_id,
                "SETTLEMENT_ERROR",
                str(exc),
            )
        async with self._lock:
            if self._active == generation.generation_id:
                self._reconcile_locked()

    async def stop(self, generation_id: str, revision: str) -> GenerationState:
        async with self._lock:
            if revision and revision != self.settings.revision:
                raise GeneratorError("expected_revision mismatch")
            generation = self.generations.get(generation_id)
            if generation is None:
                raise GeneratorError("generation not found")
            self._reconcile_locked()
            if self._active != generation_id or self._task is None:
                return generation
            generation.desired_state = "STOPPED"
            generation.actual_state = "STOPPING"
            task = self._task
            task.cancel()

        timeout = float(
            generation.bound_settings.service.get("output", {}).get(
                "grpc_timeout_seconds",
                3.0,
            )
        )
        # The caller's cancellation cannot prove that its native output settled.
        with contextlib.suppress(TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        keys = self._gateway_keys(generation)
        settled = await self.writer.wait_settled(keys, timeout=timeout)
        if settled and task.done():
            settled = await self.writer.release(generation.scenario, generation.generation_id)
        async with self._lock:
            if settled:
                self._reconcile_locked()
            else:
                generation.actual_state = "STOPPING"
                if not generation.last_error:
                    generation.last_error = "UNSETTLED_GATEWAY_OUTPUT"
                self.record(
                    "WARNING",
                    generation_id,
                    "STOP_PENDING_IO",
                    "Gateway I/O did not safely settle before stop deadline",
                )
        return generation

    async def shutdown(self) -> None:
        if self._active is not None:
            await self.stop(self._active, "")
        # The adapter must retain native ownership until its own transport settles.
        settled = await self.writer.close()
        if not settled:
            self.record(
                "WARNING",
                "runtime",
                "UNSETTLED_IO_ON_SHUTDOWN",
                "native Gateway updates unresolved at shutdown deadline",
            )

    async def _run(self, generation: GenerationState) -> None:
        generation.actual_state = "RUNNING"
        try:
            if generation.scenario.source["kind"] == "simulation":
                await self._simulate(generation)
            else:
                await self._replay(generation)
            generation.actual_state = "COMPLETED"
            generation.terminal_state = "COMPLETED"
            self.record("INFO", generation.generation_id, "COMPLETED", "source exhausted")
        except asyncio.CancelledError:
            generation.actual_state = "STOPPING"
            raise
        except Exception as exc:
            # Top-level source-task boundary: never leave a failed task in RUNNING.
            generation.actual_state = "FAILED"
            generation.terminal_state = "FAILED"
            generation.last_error = str(exc)
            self.record("ERROR", generation.generation_id, "FAILED", str(exc))
        finally:
            async with self._lock:
                # The current task is not yet done while executing finally;
                # its done-callback will make the reservation reapable later.
                if self._blocked(generation):
                    # Keep failure visible without releasing an unsettled target claim.
                    generation.actual_state = (
                        "FAILED" if generation.terminal_state == "FAILED" else "STOPPING"
                    )
                    if not generation.last_error:
                        generation.last_error = "UNSETTLED_GATEWAY_OUTPUT"
                elif generation.desired_state == "STOPPED":
                    generation.actual_state = "STOPPED"

    async def _simulate(self, generation: GenerationState) -> None:
        scenario = generation.scenario
        interval = float(scenario.source["interval_seconds"])
        seed = int(scenario.source.get("seed", 0))
        sequence = 0
        epoch = time.monotonic()
        has_physical = any(model.get("kind") == "physical" for model in scenario.models.values())
        physical = PhysicalEngine(scenario) if has_physical else None
        generation.physical_engine = physical
        random_points = {
            key: value
            for key, value in scenario.points.items()
            if isinstance(value.get("model"), dict)
        }
        while generation.desired_state == "RUNNING":
            now = time.monotonic()
            next_step = max(
                sequence + 1,
                math.floor((now - epoch) / interval) + 1,
            )
            skipped = next_step - sequence - 1
            if skipped:
                self.counters["skipped_steps"] += skipped
                self.record(
                    "WARNING",
                    generation.generation_id,
                    "SKIPPED_PERIOD",
                    str(skipped),
                )
            sequence = next_step
            planned = epoch + sequence * interval
            await asyncio.sleep(max(0.0, planned - now))
            if generation.desired_state != "RUNNING":
                return
            began = time.monotonic()
            lag = max(0.0, began - planned)
            try:
                if physical is None:
                    values = (
                        generate_configured_random(sequence, random_points)
                        if scenario.models
                        else generate_values(seed, sequence, scenario.points)
                    )
                else:
                    values = {}
                    # Reconcile skipped fixed steps without dropping physical state evolution.
                    for index in range(sequence - skipped, sequence + 1):
                        self._install_inputs(generation, index)
                        values = physical.advance(interval, index)
                    if random_points:
                        values.update(generate_configured_random(sequence, random_points))
                await self._emit(
                    generation,
                    sequence,
                    datetime.now(UTC),
                    values,
                )
            except Exception:
                self.counters.setdefault("failed_periods", 0)
                self.counters["failed_periods"] += 1
                raise
            finally:
                self.observations.append(
                    (time.monotonic(), "simulation_duration_seconds", time.monotonic() - began)
                )
                self.observations.append((time.monotonic(), "schedule_lag_seconds", lag))

    async def _replay(self, generation: GenerationState) -> None:
        scenario = generation.scenario
        source = scenario.source
        bound = generation.bound_settings
        start = datetime.fromisoformat(source["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(source["end_time"].replace("Z", "+00:00"))
        speed = float(source["speed"])
        self.replay_speed = speed
        limits = bound.service.get("replay", {})
        sequence = 0
        first_time: datetime | None = None
        epoch = 0.0
        async for record in iter_records(
            resolve_history(bound.root, source["file"]),
            start,
            end,
            read_batch_size=int(limits.get("read_batch_size", 1000)),
            max_file_bytes=int(limits.get("max_file_bytes", 268435456)),
            max_records=int(limits.get("max_records", 1000000)),
            point_types={key: str(point["data_type"]) for key, point in scenario.points.items()},
        ):
            if generation.desired_state != "RUNNING":
                return
            self.counters["replay_read"] += 1
            self.latest_replay_timestamp = record.source_time
            if first_time is None:
                first_time = record.source_time
                epoch = time.monotonic()
            deadline = replay_deadline(first_time, record.source_time, epoch, speed)
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            self.observations.append(
                (time.monotonic(), "replay_lag_seconds", max(0.0, time.monotonic() - deadline))
            )
            if generation.desired_state != "RUNNING":
                return
            key = (record.device_id, record.point_id)
            if key not in scenario.points:
                raise GeneratorError(f"CSV line {record.line_number}: unknown point {key}")
            kind = str(scenario.points[key]["data_type"])
            checked = decode_value(record.value, kind)
            sequence += 1
            status = await self._emit(
                generation,
                sequence,
                record.source_time,
                {key: checked},
            )
            self.counters.setdefault("replay_submitted", 0)
            self.counters["replay_submitted"] += 1
            if status != "SUCCESS":
                raise GeneratorError(
                    f"CSV line {record.line_number}: output {status}; replay halted"
                )
        if sequence == 0:
            raise GeneratorError("EMPTY_REPLAY_WINDOW: no source records in time range")

    @staticmethod
    def _append_record(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")

    async def _record(self, generation: GenerationState, batch: DataBatch) -> None:
        """Capture generated source values before delivery; never use Gateway ACK as truth."""
        options = generation.bound_settings.service.get("recording", {})
        if not options.get("enabled", False):
            return
        base = generation.bound_settings.root.parent.resolve()
        directory = (base / options["directory"]).resolve()
        if not directory.is_relative_to(base):
            raise GeneratorError("recording directory escapes project root")
        path = directory / f"{generation.generation_id}.jsonl"

        def encode(value: Scalar) -> Scalar | dict[str, str]:
            if isinstance(value, bytes):
                return {
                    "encoding": "base64",
                    "value": base64.b64encode(value).decode("ascii"),
                }
            return value

        payload: dict[str, object] = {
            "generation_id": batch.source_id,
            "sequence": batch.sequence,
            "produced_at": batch.produced_at.isoformat(),
            "points": [
                {
                    "device_id": point.device_id,
                    "point_id": point.point_id,
                    "source_time": point.source_time.isoformat(),
                    "value": encode(point.value),
                }
                for point in batch.points
            ],
        }
        await asyncio.to_thread(self._append_record, path, payload)

    async def _emit(
        self,
        generation: GenerationState,
        sequence: int,
        timestamp: datetime,
        values: dict[tuple[str, str], Scalar],
    ) -> str:
        batch = DataBatch(
            generation.generation_id,
            sequence,
            datetime.now(UTC),
            tuple(
                DataPoint(device, point, value, timestamp)
                for (device, point), value in values.items()
            ),
        )
        await self._record(generation, batch)
        self.counters["generated"] += len(batch.points)
        self.counters["batches"] += 1
        results = await self.writer.send(generation.scenario, batch)
        expected = sum(
            (mapping.device_id, mapping.point_id) in values
            for mapping in generation.scenario.mappings
        )
        if len(results) != expected or expected == 0:
            raise GeneratorError("Gateway delivery results incomplete")
        not_sent = {"TARGET_QUARANTINED", "TARGET_BUSY", "BACKPRESSURE_TIMEOUT"}
        self.counters["attempted"] += sum(result.error_code not in not_sent for result in results)
        confirmed = 0
        failed = 0
        unknown = 0
        for result in results:
            self.last_targets[result.target] = result
            totals = self.target_counts.setdefault(
                result.target,
                {"SUCCESS": 0, "FAILED": 0, "UNKNOWN": 0},
            )
            classification = result.state if result.state in ("SUCCESS", "FAILED") else "UNKNOWN"
            totals[classification] += 1
            self.gateway_counts[result.target.gateway_id][classification] += 1
            if result.state == "SUCCESS":
                confirmed += 1
                self.counters["confirmed"] += 1
            elif result.state == "FAILED":
                failed += 1
                self.counters["failed"] += 1
            else:
                unknown += 1
                self.counters["unknown"] += 1
                self.record(
                    "WARNING",
                    generation.generation_id,
                    "UNKNOWN_OUTPUT",
                    f"{result.target.gateway_id}:{result.target.variable_id}"
                    f" {result.error_code}",
                )
        # 仅服务端权威围栏（FAILED_PRECONDITION→LEASE_LOST）或本地无租约可致命；
        # TARGET_QUARANTINED 是"结果未知"的本地安全态，按 §9.3.2 由受控 RENEW 探测定生死。
        if any(result.error_code in {"LEASE_LOST", "LEASE_NOT_HELD"} for result in results):
            raise GeneratorError("Gateway target lease lost; refusing further output")
        if unknown:
            status = "UNKNOWN"
            self.counters["unknown_batches"] += 1
        elif confirmed == expected:
            status = "SUCCESS"
        elif confirmed and failed:
            status = "PARTIAL"
            self.counters["partial_batches"] += 1
        else:
            status = "FAILED"
        self.last_batch_status = status
        logger.info(
            "delivery batch %s confirmed=%d failed=%d unknown=%d",
            status,
            confirmed,
            failed,
            unknown,
            extra={
                "source_id": generation.generation_id,
                "batch_sequence": sequence,
                "stage": "gateway_delivery",
            },
        )
        if status != "SUCCESS":
            self.record(
                "WARNING",
                generation.generation_id,
                "BATCH_" + status,
                str(sequence),
            )
        return status
