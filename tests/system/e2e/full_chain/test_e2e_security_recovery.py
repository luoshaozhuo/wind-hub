"""R4-E：真实 full-chain Bearer 鉴权、安全拒绝、代际 fencing 与故障恢复。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import grpc
import pytest
from google.protobuf.json_format import MessageToDict
from pymodbus.client import AsyncModbusTcpClient

from contracts import commander_pb2 as cmd
from contracts import commander_pb2_grpc as cmd_rpc
from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc

from .conftest import (
    COMMANDER_GATEWAY_TOKEN_ENV,
    COMMANDER_OPS_TOKEN_ENV,
    GATEWAY_CLI_TOKEN_ENV,
    GATEWAY_GENERATOR_TOKEN_ENV,
    GENERATOR_CLI_TOKEN_ENV,
    GENERATOR_GATEWAY_TOKEN_ENV,
    FullChainHarness,
)
from .integration_segments import bearer_channel

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]


async def _read_gateway(
    harness: FullChainHarness, gateway_id: str, variable_id: str
) -> dict[str, Any]:
    async with bearer_channel(
        harness.endpoints["gateway"], harness.tokens[GATEWAY_CLI_TOKEN_ENV]
    ) as channel:
        reply = await gw_rpc.IndustrialGatewayServiceStub(channel).ReadVariables(
            gw.ReadVariablesRequest(gateway_id=gateway_id, variable_ids=[variable_id]),
            timeout=10,
        )
        assert len(reply.values) == 1
        return MessageToDict(
            reply.values[0],
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )


def _measurement(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "queried_at"}


async def _wait_gateway_measurement(
    harness: FullChainHarness, gateway_id: str, variable_id: str
) -> dict[str, Any]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        value = await _read_gateway(harness, gateway_id, variable_id)
        if "value" in value and value.get("quality") == "GOOD":
            return value
        await asyncio.sleep(0.2)
    raise AssertionError(f"Gateway value not initialized: {gateway_id}/{variable_id}")


async def _start_generation(
    harness: FullChainHarness, scenario: str = "wind-plant-physical"
) -> str:
    started = await harness.cli_call(
        harness.cli_rt,
        "data-generator",
        "start",
        scenario,
        "--scenario-version",
        "1.0.0",
        "--yes",
    )
    generation_id = str(started["data"]["generation_id"])
    assert generation_id
    harness.generation_id = generation_id
    return generation_id


async def _stop_generation(harness: FullChainHarness, generation_id: str | None) -> None:
    if generation_id is None:
        return
    await harness.cli_call(
        harness.cli_rt,
        "data-generator",
        "stop",
        generation_id,
        "--yes",
        check=False,
    )
    if harness.generation_id == generation_id:
        harness.generation_id = None


async def _wait_terminal_generation(harness: FullChainHarness, generation_id: str) -> None:
    await harness.poll(
        harness.cli_rt,
        "data-generator",
        "status",
        generation_id,
        predicate=lambda data: data.get("generation", {}).get("actual_state")
        in {"COMPLETED", "FAILED"},
        timeout=45,
    )


async def _wait_recording(harness: FullChainHarness, generation_id: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        path = harness.recording_path(generation_id)
        if path.is_file() and path.stat().st_size:
            return harness.read_recording(generation_id)
        await asyncio.sleep(0.2)
    raise AssertionError(f"recording was not produced: {generation_id}")


async def _input_execution(
    harness: FullChainHarness, generation_id: str, request_id: str
) -> dg.GetInputExecutionResponse:
    async with bearer_channel(
        harness.endpoints["data-generator"], harness.tokens[GATEWAY_GENERATOR_TOKEN_ENV]
    ) as channel:
        return await dg_rpc.DataGeneratorServiceStub(channel).GetInputExecution(
            dg.GetInputExecutionRequest(generation_id=generation_id, request_id=request_id),
            timeout=10,
        )


def _last_cli_returncode(harness: FullChainHarness) -> int:
    rows = [line for line in harness.cli.log.read_text(encoding="utf-8").splitlines() if line]
    assert rows
    return int(json.loads(rows[-1])["returncode"])


@pytest.mark.parametrize("kind", ["missing", "malformed", "unregistered"])
async def test_r4e_bearer_rejects_bad_credentials_without_write(
    full_chain: FullChainHarness, kind: str
) -> None:
    request_id = f"r4e-unauthorized-{uuid4()}"
    if kind == "missing":
        service = "commander"
        target_rpc = "CommanderService.SubmitCommand"
        metadata: tuple[tuple[str, str], ...] = ()
        device = await full_chain.cli_call(full_chain.cli_rt, "commander", "device", "PPC_SIM01")
        revision = str(device["data"]["device"]["revision"])
        before = {"request_result": "NOT_SUBMITTED"}
    else:
        service = "gateway"
        target_rpc = "IndustrialGatewayService.UpdateVariables"
        metadata = (
            ("authorization", "not-a-bearer-credential")
            if kind == "malformed"
            else ("authorization", f"Bearer {uuid4().hex}"),
        )
        before = await _read_gateway(full_chain, "gw-gw02", "a02_dm_pitchangle1")
    actual_endpoint = full_chain.endpoints[service]
    async with grpc.aio.insecure_channel(actual_endpoint) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            if kind == "missing":
                await cmd_rpc.CommanderServiceStub(channel).SubmitCommand(
                    cmd.SubmitCommandRequest(
                        device_id="PPC_SIM01",
                        point_id="agc_enable",
                        value=common.ScalarValue(uint64_value=1),
                        expected_revision=revision,
                        request_id=request_id,
                    ),
                    timeout=10,
                    metadata=metadata,
                )
            else:
                await gw_rpc.IndustrialGatewayServiceStub(channel).UpdateVariables(
                    gw.UpdateVariablesRequest(
                        gateway_id="gw-gw02",
                        lease_token="authentication-must-fail-before-lease-validation",
                        values=[
                            gw.VariableUpdate(
                                variable_id="a02_dm_pitchangle1",
                                value=common.ScalarValue(double_value=99.0),
                            )
                        ],
                    ),
                    timeout=10,
                    metadata=metadata,
                )
    assert exc_info.value.code() is grpc.StatusCode.UNAUTHENTICATED
    if kind == "missing":
        readback = await full_chain.cli_call(
            full_chain.cli_rt, "commander", "request-result", request_id, check=False
        )
        assert readback["status"] == "FAILURE"
        assert readback["error_code"] == "NOT_FOUND"
        after: Any = {"request_result": readback["error_code"]}
        no_side_effect = "commander_request_absent"
    else:
        after = await _read_gateway(full_chain, "gw-gw02", "a02_dm_pitchangle1")
        assert _measurement(after) == _measurement(before)
        readback = after
        no_side_effect = "gateway_value_unchanged"
    full_chain.record(
        "R4-E-BEARER-AUTH",
        kind=kind,
        service=service,
        actual_endpoint=actual_endpoint,
        target_rpc=target_rpc,
        grpc_status=exc_info.value.code().name,
        request_id=request_id if kind == "missing" else None,
        before=before,
        after=after,
        readback=readback,
        assertions={"authentication_rejected": True, no_side_effect: True},
    )


async def test_r4e_commander_identity_cannot_write_gateway_or_generator(
    full_chain: FullChainHarness,
) -> None:
    generation_id: str | None = None
    request_id = f"r4e-unauthorized-{uuid4()}"
    try:
        generation_id = await _start_generation(full_chain)
        gateway_before = await _wait_gateway_measurement(full_chain, "gw-wtg_sim01", "wind_speed")
        token = full_chain.tokens[COMMANDER_GATEWAY_TOKEN_ENV]
        async with bearer_channel(full_chain.endpoints["gateway"], token) as gateway_channel:
            with pytest.raises(grpc.aio.AioRpcError) as gateway_error:
                await gw_rpc.IndustrialGatewayServiceStub(gateway_channel).UpdateVariables(
                    gw.UpdateVariablesRequest(
                        gateway_id="gw-wtg_sim01",
                        lease_token="authorization-precedes-lease-validation",
                        values=[
                            gw.VariableUpdate(
                                variable_id="wind_speed",
                                value=common.ScalarValue(double_value=77.0),
                            )
                        ],
                    ),
                    timeout=10,
                )
        assert gateway_error.value.code() is grpc.StatusCode.PERMISSION_DENIED

        async with bearer_channel(
            full_chain.endpoints["data-generator"], token
        ) as generator_channel:
            with pytest.raises(grpc.aio.AioRpcError) as generator_error:
                await dg_rpc.DataGeneratorServiceStub(generator_channel).UpdateVariables(
                    dg.UpdateVariablesRequest(
                        generation_id=generation_id,
                        device_id="WTG_SIM01",
                        request_id=request_id,
                        values=[
                            dg.VariableUpdate(
                                point_id="wind_speed_mean",
                                value=common.ScalarValue(double_value=77.0),
                            )
                        ],
                    ),
                    timeout=10,
                )
        assert generator_error.value.code() is grpc.StatusCode.UNAUTHENTICATED

        gateway_after = await _wait_gateway_measurement(full_chain, "gw-wtg_sim01", "wind_speed")
        # 活跃 Generation 会合法刷新 sample_time；拒绝写的不变量是值与质量。
        assert gateway_after["value"] == gateway_before["value"]
        assert gateway_after["quality"] == gateway_before["quality"]
        execution = await _input_execution(full_chain, generation_id, request_id)
        assert execution.state == "UNKNOWN"
        assert execution.effective_step == 0
        full_chain.record(
            "R4-E-COMMANDER-IDENTITY-DENIED",
            generation_id=generation_id,
            request_id=request_id,
            gateway_status=gateway_error.value.code().name,
            generator_status=generator_error.value.code().name,
            gateway_before=gateway_before,
            gateway_after=gateway_after,
            generator_execution=execution.state,
            assertions={
                "gateway_permission_denied_and_value_unchanged": True,
                "generator_permission_denied_and_no_execution": True,
            },
        )
    finally:
        await _stop_generation(full_chain, generation_id)


async def test_r4e_stopped_g1_write_rejected_and_g2_has_no_replay(
    full_chain: FullChainHarness,
) -> None:
    g1: str | None = None
    g2: str | None = None
    request_id = f"r4e-old-generation-{uuid4()}"
    try:
        g1 = await _start_generation(full_chain)
        await _stop_generation(full_chain, g1)
        rejected = await full_chain.cli_call(
            full_chain.cli_rt,
            "data-generator",
            "variables",
            "update",
            "--generation-id",
            g1,
            "--device-id",
            "WTG_SIM01",
            "--request-id",
            request_id,
            "--point-id",
            "wind_speed_mean",
            "--value",
            "88",
            "--yes",
            check=False,
        )
        assert rejected["status"] == "FAILURE"
        assert rejected["data"] is None
        assert rejected["error_code"] == "PermissionError"
        async with bearer_channel(
            full_chain.endpoints["data-generator"], full_chain.tokens[GENERATOR_CLI_TOKEN_ENV]
        ) as channel:
            rpc_rejected = await dg_rpc.DataGeneratorServiceStub(channel).UpdateVariables(
                dg.UpdateVariablesRequest(
                    generation_id=g1,
                    device_id="WTG_SIM01",
                    request_id=request_id,
                    values=[
                        dg.VariableUpdate(
                            point_id="wind_speed_mean",
                            value=common.ScalarValue(double_value=88.0),
                        )
                    ],
                ),
                timeout=10,
            )
        assert rpc_rejected.accepted is False
        assert rpc_rejected.error_code == "INPUT_REJECTED"
        old_execution = await _input_execution(full_chain, g1, request_id)
        assert old_execution.state == "UNKNOWN"

        g2 = await _start_generation(full_chain)
        assert g2 != g1
        replay_execution = await _input_execution(full_chain, g2, request_id)
        assert replay_execution.state == "UNKNOWN"
        assert replay_execution.effective_step == 0
        rows = await _wait_recording(full_chain, g2)
        wind_values = [
            float(point["value"])
            for row in rows
            for point in row["points"]
            if point["device_id"] == "WTG_SIM01" and point["point_id"] == "wind_speed"
        ]
        assert wind_values
        assert set(wind_values) == {10.0}
        full_chain.record(
            "R4-E-GENERATION-FENCING",
            g1=g1,
            g2=g2,
            request_id=request_id,
            rejected_status=rejected["status"],
            rejected_error_code=rpc_rejected.error_code,
            cli_rejection=rejected["error_code"],
            old_execution=old_execution.state,
            g2_replay_execution=replay_execution.state,
            g2_wind_values=wind_values,
            assertions={"g1_input_rejected": True, "g2_has_no_old_request_side_effect": True},
        )
    finally:
        await _stop_generation(full_chain, g1)
        await _stop_generation(full_chain, g2)


async def test_r4e_gateway_lease_expiry_renew_lost_and_update_precondition(
    full_chain: FullChainHarness,
) -> None:
    before = await _read_gateway(full_chain, "gw-gw02", "a02_dm_pitchangle1")
    async with bearer_channel(
        full_chain.endpoints["gateway"], full_chain.tokens[GENERATOR_GATEWAY_TOKEN_ENV]
    ) as channel:
        stub = gw_rpc.IndustrialGatewayServiceStub(channel)
        owner_id = f"r4e-lease-{uuid4()}"
        claim = await stub.ClaimTargets(
            gw.ClaimTargetsRequest(
                gateway_id="gw-gw02",
                owner_id=owner_id,
                variable_ids=["a02_dm_pitchangle1"],
                action="ACQUIRE",
                lease_ms=1000,
            ),
            timeout=10,
        )
        assert claim.accepted is True
        assert claim.lease_token
        await asyncio.sleep(1.2)
        renewed = await stub.ClaimTargets(
            gw.ClaimTargetsRequest(
                gateway_id="gw-gw02",
                owner_id=owner_id,
                variable_ids=["a02_dm_pitchangle1"],
                action="RENEW",
                lease_token=claim.lease_token,
                lease_ms=1000,
            ),
            timeout=10,
        )
        assert renewed.accepted is False
        assert renewed.error_code == "LEASE_LOST"
        with pytest.raises(grpc.aio.AioRpcError) as update_error:
            await stub.UpdateVariables(
                gw.UpdateVariablesRequest(
                    gateway_id="gw-gw02",
                    lease_token=claim.lease_token,
                    values=[
                        gw.VariableUpdate(
                            variable_id="a02_dm_pitchangle1",
                            value=common.ScalarValue(double_value=66.0),
                        )
                    ],
                ),
                timeout=10,
            )
    assert update_error.value.code() is grpc.StatusCode.FAILED_PRECONDITION
    assert "LEASE_LOST" in update_error.value.details()
    after = await _read_gateway(full_chain, "gw-gw02", "a02_dm_pitchangle1")
    assert _measurement(after) == _measurement(before)
    full_chain.record(
        "R4-E-GATEWAY-LEASE-EXPIRED",
        lease_ms=1000,
        renew_accepted=renewed.accepted,
        renew_error_code=renewed.error_code,
        update_status=update_error.value.code().name,
        update_details=update_error.value.details(),
        before=before,
        after=after,
        assertions={"renew_lease_lost": True, "update_failed_precondition_no_write": True},
    )


async def test_r4e_commander_stop_unknown_restart_no_replay_and_new_request_independent(
    full_chain: FullChainHarness,
) -> None:
    generation_id: str | None = None
    commander_stopped = False
    old_request_id = f"r4e-commander-down-{uuid4()}"
    new_request_id = f"r4e-commander-new-{uuid4()}"
    try:
        generation_id = await _start_generation(full_chain)
        device = await full_chain.cli_call(full_chain.cli_rt, "commander", "device", "PPC_SIM01")
        revision = str(device["data"]["device"]["revision"])
        await full_chain.cli_call(full_chain.cli_rt, "service", "stop", "commander", "--yes")
        commander_stopped = True
        old = await full_chain.cli_call(
            full_chain.cli_rt,
            "commander",
            "write",
            "PPC_SIM01",
            "agc_enable",
            "1",
            "--expected-revision",
            revision,
            "--expires-at",
            (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
            "--request-id",
            old_request_id,
            "--yes",
            check=False,
        )
        old_returncode = _last_cli_returncode(full_chain)
        assert old["status"] == "UNKNOWN"
        assert old["error_code"] == "UNAVAILABLE"
        assert old_returncode == 3

        await full_chain.service("start", "commander")
        commander_stopped = False
        old_lookup = await full_chain.cli_call(
            full_chain.cli_rt, "commander", "request-result", old_request_id, check=False
        )
        assert old_lookup["status"] == "FAILURE"
        assert old_lookup["error_code"] == "NOT_FOUND"

        new = await full_chain.cli_call(
            full_chain.cli_rt,
            "commander",
            "write",
            "PPC_SIM01",
            "agc_enable",
            "0",
            "--expected-revision",
            revision,
            "--expires-at",
            (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
            "--request-id",
            new_request_id,
            "--yes",
        )
        new_command_id = str(new["data"]["command_id"])
        assert new["data"]["accepted"] is True
        assert new["data"]["request_id"] == new_request_id != old_request_id
        completed = await full_chain.poll(
            full_chain.cli_rt,
            "commander",
            "result",
            new_command_id,
            predicate=lambda item: item.get("state") in {"SUCCEEDED", "FAILED", "UNKNOWN"},
            timeout=60,
        )
        assert completed["state"] == "SUCCEEDED"
        assert completed["outcome"] == "CONFIRMED_SUCCESS"
        old_after = await full_chain.cli_call(
            full_chain.cli_rt, "commander", "request-result", old_request_id, check=False
        )
        assert old_after["error_code"] == "NOT_FOUND"
        full_chain.record(
            "R4-E-COMMANDER-RESTART",
            generation_id=generation_id,
            old_request_id=old_request_id,
            old_status=old["status"],
            old_returncode=old_returncode,
            old_lookup_after_restart=old_lookup["error_code"],
            new_request_id=new_request_id,
            new_command_id=new_command_id,
            new_result=completed["state"],
            old_lookup_after_new_success=old_after["error_code"],
            assertions={
                "unknown_exit_3": True,
                "no_automatic_replay": True,
                "new_request_independent": True,
            },
        )
    finally:
        if commander_stopped:
            await full_chain.cli_call(
                full_chain.cli_rt, "service", "start", "commander", "--yes", check=False
            )
        await _stop_generation(full_chain, generation_id)


async def test_r4e_modbus_illegal_address_and_unset_scalar_reject_without_write(
    full_chain: FullChainHarness,
) -> None:
    request_id = f"r4e-unset-scalar-{uuid4()}"
    client = AsyncModbusTcpClient("127.0.0.1", port=full_chain.modbus_ports["PPC_SIM01"], timeout=5)
    await client.connect()
    try:
        before = await client.read_holding_registers(0, count=1, device_id=1)
        assert before.isError() is False
        illegal = await client.write_register(65535, 1, device_id=1)
        assert illegal.isError() is True
        assert illegal.exception_code == 2
        after_illegal = await client.read_holding_registers(0, count=1, device_id=1)
        assert after_illegal.isError() is False
        assert after_illegal.registers == before.registers
    finally:
        client.close()

    device = await full_chain.cli_call(full_chain.cli_rt, "commander", "device", "PPC_SIM01")
    revision = str(device["data"]["device"]["revision"])
    async with bearer_channel(
        full_chain.endpoints["commander"], full_chain.tokens[COMMANDER_OPS_TOKEN_ENV]
    ) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as unset_error:
            await cmd_rpc.CommanderServiceStub(channel).SubmitCommand(
                cmd.SubmitCommandRequest(
                    device_id="PPC_SIM01",
                    point_id="agc_enable",
                    value=common.ScalarValue(),
                    expected_revision=revision,
                    request_id=request_id,
                ),
                timeout=10,
            )
    assert unset_error.value.code() is grpc.StatusCode.INVALID_ARGUMENT
    lookup = await full_chain.cli_call(
        full_chain.cli_rt, "commander", "request-result", request_id, check=False
    )
    assert lookup["status"] == "FAILURE"
    assert lookup["error_code"] == "NOT_FOUND"

    client = AsyncModbusTcpClient("127.0.0.1", port=full_chain.modbus_ports["PPC_SIM01"], timeout=5)
    await client.connect()
    try:
        after_unset = await client.read_holding_registers(0, count=1, device_id=1)
        assert after_unset.isError() is False
        assert after_unset.registers == before.registers
    finally:
        client.close()
    full_chain.record(
        "R4-E-MODBUS-AND-SCALAR-REJECTION",
        illegal_address=65535,
        modbus_exception_code=illegal.exception_code,
        unset_scalar_request_id=request_id,
        unset_scalar_status=unset_error.value.code().name,
        unset_scalar_lookup=lookup["error_code"],
        registers_before=before.registers,
        registers_after_illegal=after_illegal.registers,
        registers_after_unset=after_unset.registers,
        assertions={"illegal_address_no_write": True, "unset_scalar_no_command_or_write": True},
    )


async def test_r4e_corrupt_g1_recording_fails_closed_and_g2_parses_independently(
    full_chain: FullChainHarness,
) -> None:
    g1: str | None = None
    g2: str | None = None
    try:
        g1 = await _start_generation(full_chain, "e2e-replay-1x")
        await _wait_terminal_generation(full_chain, g1)
        g1_rows = harness_rows = full_chain.read_recording(g1)
        assert harness_rows
        g1_path = full_chain.recording_path(g1)
        g1_path.write_bytes(b'{"generation_id":' + g1_path.read_bytes())
        with pytest.raises(json.JSONDecodeError):
            full_chain.read_recording(g1)

        g2 = await _start_generation(full_chain, "e2e-replay-1x")
        assert g2 != g1
        await _wait_terminal_generation(full_chain, g2)
        g2_rows = full_chain.read_recording(g2)
        assert g2_rows
        assert [point["value"] for row in g2_rows for point in row["points"]] == [5.5, 6.0, 7.5]
        assert full_chain.recording_path(g2) != g1_path
        full_chain.record(
            "R4-E-RECORDING-CORRUPTION-ISOLATION",
            g1=g1,
            g2=g2,
            g1_valid_rows_before_corruption=len(g1_rows),
            g1_corruption="truncated JSON object prepended to original JSONL",
            g1_reader_error="JSONDecodeError",
            g2_rows=len(g2_rows),
            g2_values=[point["value"] for row in g2_rows for point in row["points"]],
            assertions={"g1_parse_fail_closed": True, "g2_independent_parse": True},
        )
    finally:
        await _stop_generation(full_chain, g1)
        await _stop_generation(full_chain, g2)
