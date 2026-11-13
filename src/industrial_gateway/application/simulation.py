"""Commander-authorized simulation execution with bounded in-process deduplication."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

logger = logging.getLogger(__name__)

from industrial_gateway.application.ports.simulation import SimulationInputPort
from industrial_gateway.application.variable_store import Scalar


@dataclass(frozen=True, slots=True)
class SimulationCommand:
    caller: str
    gateway_id: str
    device_id: str
    point_id: str
    value: Scalar
    request_id: str
    expected_revision: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SimulationOutcome:
    request_id: str
    accepted: bool
    execution_id: str
    state: str
    error_code: str = ""
    message: str = ""
    effective_step: int = 0


class SimulationController:
    """Keep active request identity stable even when a gRPC caller disconnects."""

    def __init__(
        self,
        input_factory: Callable[[str], SimulationInputPort],
        *,
        capacity: int = 4096,
    ) -> None:
        self._input_factory = input_factory
        self._capacity = capacity
        self._requests: dict[
            tuple[str, str, str], tuple[SimulationCommand, asyncio.Task[SimulationOutcome]]
        ] = {}
        self._mutex = asyncio.Lock()
        self._target_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def execute(self, command: SimulationCommand) -> SimulationOutcome:
        key = (command.caller, command.gateway_id, command.request_id)
        async with self._mutex:
            previous = self._requests.get(key)
            if previous is not None:
                original, task = previous
                if original != command:
                    return SimulationOutcome(
                        request_id=command.request_id,
                        accepted=False,
                        execution_id="",
                        state="REJECTED",
                        error_code="IDEMPOTENCY_CONFLICT",
                    )
            else:
                if len(self._requests) >= self._capacity:
                    return SimulationOutcome(
                        request_id=command.request_id,
                        accepted=False,
                        execution_id="",
                        state="REJECTED",
                        error_code="EXECUTION_CAPACITY_EXCEEDED",
                    )
                target = (command.gateway_id, command.point_id)
                lock = self._target_locks.setdefault(target, asyncio.Lock())
                execution_id = uuid4().hex
                task = asyncio.create_task(self._apply(command, execution_id, lock))
                self._requests[key] = (command, task)
        # Transport cancellation cannot cancel a possibly submitted Generator input.
        return await asyncio.shield(task)

    async def result(
        self, *, caller: str, gateway_id: str, request_id: str
    ) -> SimulationOutcome:
        key = (caller, gateway_id, request_id)
        async with self._mutex:
            existing = self._requests.get(key)
        if existing is None:
            return SimulationOutcome(
                request_id=request_id,
                accepted=False,
                execution_id="",
                state="UNKNOWN",
                error_code="EXECUTION_NOT_OBSERVED",
            )
        task = existing[1]
        if not task.done():
            return SimulationOutcome(
                request_id=request_id,
                accepted=True,
                execution_id="",
                state="PENDING",
            )
        if task.cancelled():
            return SimulationOutcome(request_id, True, "", "UNKNOWN", "EXECUTION_CANCELLED")
        try:
            return task.result()
        except Exception as exc:
            return SimulationOutcome(request_id, True, "", "UNKNOWN", type(exc).__name__)

    async def _apply(
        self, command: SimulationCommand, execution_id: str, lock: asyncio.Lock
    ) -> SimulationOutcome:
        async with lock:
            remaining = (command.expires_at - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                return SimulationOutcome(
                    command.request_id, False, execution_id, "REJECTED", "EXPIRED"
                )
            client: SimulationInputPort | None = None
            try:
                client = self._input_factory(command.gateway_id)
                client.start()
                receipt = await client.submit(
                    device_id=command.device_id,
                    point_id=command.point_id,
                    value=command.value,
                    request_id=command.request_id,
                    timeout=remaining,
                )
                if not receipt.accepted:
                    return SimulationOutcome(
                        command.request_id,
                        False,
                        execution_id,
                        "REJECTED",
                        receipt.error_code,
                        receipt.message,
                    )
                if receipt.effective_step <= 0:
                    return SimulationOutcome(
                        command.request_id,
                        True,
                        execution_id,
                        "UNKNOWN",
                        "NO_EFFECTIVE_STEP",
                    )
                return SimulationOutcome(
                    command.request_id,
                    True,
                    execution_id,
                    "ACCEPTED",
                    effective_step=receipt.effective_step,
                )
            except (ValueError, PermissionError) as exc:
                return SimulationOutcome(
                    command.request_id,
                    False,
                    execution_id,
                    "REJECTED",
                    type(exc).__name__,
                )
            except Exception as exc:
                # The transport may have delivered the input before failing.
                return SimulationOutcome(
                    command.request_id,
                    True,
                    execution_id,
                    "UNKNOWN",
                    type(exc).__name__,
                )
            finally:
                if client is not None:
                    try:
                        await client.close()
                    except Exception:
                        logger.exception("simulation input channel could not close")
