"""One simulation execution path for physical and random device models."""

from __future__ import annotations

from collections.abc import Callable

from data_generator.application.model.random import generate as generate_configured_random
from data_generator.application.models import generate_values
from data_generator.application.physical_engine import PhysicalEngine
from data_generator.domain.model import Scalar
from data_generator.domain.scenario import Scenario

PointValues = dict[tuple[str, str], Scalar]


class SimulationCoordinator:
    """Advance a Generation consistently even when scheduler periods are skipped."""

    def __init__(self, scenario: Scenario) -> None:
        self._scenario = scenario
        self.physical = (
            PhysicalEngine(scenario)
            if any(model.get("kind") == "physical" for model in scenario.models.values())
            else None
        )
        self._random_points = {
            key: value
            for key, value in scenario.points.items()
            if isinstance(value.get("model"), dict)
        }

    def advance(
        self,
        *,
        step: int,
        skipped: int,
        interval: float,
        seed: int,
        install_inputs: Callable[[int], None],
    ) -> tuple[PointValues, int | None]:
        """Evaluate an entire effective step with one deterministic output contract."""
        physical = self.physical
        values: PointValues
        if physical is None:
            values = (
                generate_configured_random(step, self._random_points)
                if self._scenario.models
                else generate_values(seed, step, self._scenario.points)
            )
            return values, None
        values = {}
        for index in range(step - skipped, step + 1):
            install_inputs(index)
            values = physical.advance(interval, index)
        if self._random_points:
            values.update(generate_configured_random(step, self._random_points))
        return values, step
