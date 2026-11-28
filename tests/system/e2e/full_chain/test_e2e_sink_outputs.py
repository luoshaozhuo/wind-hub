"""E2E-07: verify all four Sink outputs from one real full-chain generation."""

from __future__ import annotations

import pytest

from .conftest import (
    SIM_DEVICES,
    SINK_TARGET_POINT,
    decode_kafka_wide_point,
    wait_sink_modbus_float,
)

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def test_e2e_07_four_sink_outputs(full_chain) -> None:  # noqa: ANN001
    """Read Gateway, Modbus, Redis and Kafka independently after one source batch."""
    harness = full_chain
    generation_id: str | None = None
    try:
        for config in (harness.cli_rt, harness.cli_rel):
            for device_id in SIM_DEVICES:
                await harness.cli_call(config, "collector", "start", device_id, "--yes")

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

        assert harness.sink is not None
        expected = 10.0
        redis_value = await harness.sink.wait_redis_value(
            harness.redis_key("WTG_SIM01", "wind_speed"),
            timeout=90,
            match=lambda value: abs(float(value.get("value", 0.0)) - expected) < 1e-3,
        )
        assert abs(float(redis_value["value"]) - expected) < 1e-3
        assert redis_value["quality"] == "good"

        # OUT-01: Sink GatewayOutput writes an isolated target variable.
        gateway = await harness.poll(
            harness.cli_rt,
            "gateway",
            "read",
            "gw-wtg_sim01",
            SINK_TARGET_POINT,
            predicate=lambda data: bool(data.get("values"))
            and data["values"][0]["quality"] == "GOOD",
            timeout=90,
        )
        assert abs(float(gateway["values"][0]["value"]["double_value"]) - expected) < 1e-3
        assert gateway["values"][0]["quality"] == "GOOD"

        # OUT-02: independent Modbus client reads Sink's own output server.
        modbus_value, modbus_quality = await wait_sink_modbus_float(
            harness.sink_modbus_port, expected, timeout=90
        )
        assert modbus_quality == 0

        # OUT-04: the reliable-only topic carries strict device-wide JSON v3.
        await harness.poll(
            harness.cli_rel,
            "collector",
            "latest",
            "WTG_SIM01",
            "wind_speed",
            predicate=lambda data: any(
                sample.get("point_id") == "wind_speed"
                and abs(float(sample["value"]["double_value"]) - expected) < 1e-3
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
        matching = [item for item in payloads if item["data_level"] == "protocol"]
        assert matching, payloads
        assert any(abs(float(item["value"]) - expected) < 1e-3 for item in matching), [
            item["value"] for item in matching
        ]
        assert len({item["sample_time"] for item in matching}) == len(matching)

        harness.record(
            "E2E-07",
            generation_id=generation_id,
            redis_value=expected,
            gateway_value=gateway["values"][0]["value"],
            modbus_value=modbus_value,
            kafka_messages=len(matching),
        )
    finally:
        if generation_id is not None:
            await harness.cli_call(
                harness.cli_rt,
                "data-generator",
                "stop",
                generation_id,
                "--yes",
                check=False,
            )
            harness.generation_id = None
        for config in (harness.cli_rt, harness.cli_rel):
            for device_id in SIM_DEVICES:
                await harness.cli_call(config, "collector", "stop", device_id, "--yes", check=False)
