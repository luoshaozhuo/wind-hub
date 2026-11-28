"""E2E-01: change wind input in one running physical generation."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from .conftest import SINK_TARGET_POINT, decode_kafka_wide_point, wait_sink_modbus_float

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def test_e2e_01_wind_step(full_chain: Any) -> None:
    harness = full_chain
    assert harness.sink is not None
    generation_id: str | None = None
    try:
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "start", "WTG_SIM01", "--yes")
        started = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "start",
            "wind-plant-physical",
            "--scenario-version",
            "1.0.0",
            "--yes",
        )
        generation_id = str(started["data"]["generation_id"])
        harness.generation_id = generation_id
        key = harness.redis_key("WTG_SIM01", "wind_speed")
        before = await harness.sink.wait_redis_value(
            key, timeout=90, match=lambda item: abs(float(item["value"]) - 10.0) < 1e-3
        )
        request_id = str(uuid4())
        updated = await harness.cli_call(
            harness.cli_rt,
            "data-generator",
            "variables",
            "update",
            "--generation-id",
            generation_id,
            "--device-id",
            "WTG_SIM01",
            "--request-id",
            request_id,
            "--point-id",
            "wind_speed_mean",
            "--value",
            "12.0",
            "--yes",
        )
        assert updated["data"]["accepted"] is True
        assert int(updated["data"]["effective_step"]) > 0
        after = await harness.sink.wait_redis_value(
            key,
            timeout=90,
            match=lambda item: abs(float(item["value"]) - 12.0) < 1e-3
            and item["sample_id"] != before["sample_id"],
        )
        gateway = await harness.poll(
            harness.cli_rt,
            "gateway",
            "read",
            "gw-wtg_sim01",
            SINK_TARGET_POINT,
            predicate=lambda data: bool(data.get("values"))
            and abs(float(data["values"][0]["value"].get("double_value", 0)) - 12.0) < 1e-3
            and data["values"][0]["quality"] == "GOOD",
            timeout=90,
        )
        modbus_value, quality = await wait_sink_modbus_float(
            harness.sink_modbus_port, 12.0, timeout=90
        )
        assert quality == 0
        await harness.poll(
            harness.cli_rel,
            "collector",
            "latest",
            "WTG_SIM01",
            "wind_speed",
            predicate=lambda data: any(
                sample.get("point_id") == "wind_speed"
                and abs(float(sample["value"].get("double_value", 0)) - 12.0) < 1e-3
                for sample in data.get("samples", [])
            ),
            timeout=90,
        )
        records = await harness.sink.wait_kafka_messages(
            min_messages=100, timeout=90, offset_reset="latest"
        )
        payloads = [
            item
            for record in records
            if (
                item := decode_kafka_wide_point(
                    record, device_id="WTG_SIM01", point_id="wind_speed"
                )
            )
            is not None
        ]
        assert any(abs(float(item["value"]) - 12.0) < 1e-3 for item in payloads)
        rows = harness.read_recording(generation_id)
        wind = [
            p["value"]
            for row in rows
            for p in row["points"]
            if p["device_id"] == "WTG_SIM01" and p["point_id"] == "wind_speed"
        ]
        assert 10.0 in wind and 12.0 in wind and wind.index(10.0) < wind.index(12.0)
        harness.record(
            "E2E-01-STEP",
            generation_id=generation_id,
            request_id=request_id,
            effective_step=updated["data"]["effective_step"],
            before=before["value"],
            after=after["value"],
            gateway=gateway["values"][0]["value"],
            modbus=modbus_value,
        )
    finally:
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt, "data-generator", "stop", generation_id, "--yes", check=False
            )
            harness.generation_id = None
        for config in (harness.cli_rt, harness.cli_rel):
            await harness.cli_call(config, "collector", "stop", "WTG_SIM01", "--yes", check=False)
