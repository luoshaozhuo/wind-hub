"""E2E-05 (seeded part): deterministic physical random-input recordings."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from .conftest import SIM_DEVICES

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]


def _wind_sequence(harness: Any, generation_id: str) -> list[float]:
    return [
        float(point["value"])
        for row in harness.read_recording(generation_id)
        for point in row.get("points", [])
        if point.get("device_id") == "WTG_SIM01" and point.get("point_id") == "wind_speed"
    ]


async def test_e2e_05_seeded_physical_input_is_reproducible(full_chain: Any) -> None:
    """Two CLI generations with the same weather seed produce the same truth series."""
    harness = full_chain
    generation_ids: list[str] = []
    try:
        for config in (harness.cli_rt, harness.cli_rel):
            for device_id in SIM_DEVICES:
                await harness.cli_call(config, "collector", "start", device_id, "--yes")

        for _ in range(2):
            started = await harness.cli_call(
                harness.cli_rt,
                "data-generator",
                "start",
                "e2e-wind-seeded",
                "--scenario-version",
                "1.0.0",
                "--yes",
            )
            generation_id = str(started["data"]["generation_id"])
            generation_ids.append(generation_id)
            harness.generation_id = generation_id
            await asyncio.sleep(0.7)
            await harness.cli_call(harness.cli_rt, "data-generator", "stop", generation_id, "--yes")
            harness.generation_id = None

        first, second = (_wind_sequence(harness, generation_id) for generation_id in generation_ids)
        assert len(first) >= 8 and len(second) >= 8
        assert first[:8] == pytest.approx(second[:8], abs=1e-9)
        assert len({round(value, 8) for value in first[:8]}) > 1
        harness.record(
            "E2E-05-SEED",
            generations=generation_ids,
            samples=8,
            sequence=first[:8],
        )
    finally:
        if harness.generation_id is not None:
            await harness.cli_call(
                harness.cli_rt,
                "data-generator",
                "stop",
                harness.generation_id,
                "--yes",
                check=False,
            )
            harness.generation_id = None
        for config in (harness.cli_rt, harness.cli_rel):
            for device_id in SIM_DEVICES:
                await harness.cli_call(config, "collector", "stop", device_id, "--yes", check=False)
