"""R4-E：真实 full-chain mTLS、安全拒绝、代际 fencing 与故障恢复。"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import grpc
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from google.protobuf.json_format import MessageToDict
from pymodbus.client import AsyncModbusTcpClient

from cli.runtime import Runtime
from contracts import commander_pb2 as cmd
from contracts import commander_pb2_grpc as cmd_rpc
from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg
from contracts import data_generator_pb2_grpc as dg_rpc
from contracts import industrial_gateway_pb2 as gw
from contracts import industrial_gateway_pb2_grpc as gw_rpc

from .conftest import FullChainHarness

pytestmark = [
    pytest.mark.system,
    pytest.mark.real_service,
    pytest.mark.docker,
    pytest.mark.slow,
]


def _client_credentials(harness: FullChainHarness, name: str) -> grpc.ChannelCredentials:
    certificate, key = harness.pki["clients"][name]
    return grpc.ssl_channel_credentials(
        root_certificates=harness.pki["ca"].read_bytes(),
        private_key=key.read_bytes(),
        certificate_chain=certificate.read_bytes(),
    )


async def _read_gateway(
    harness: FullChainHarness, gateway_id: str, variable_id: str
) -> dict[str, Any]:
    runtime = Runtime(harness.cli_rt, "json")
    try:
        reply = await gw_rpc.IndustrialGatewayServiceStub(runtime.channel("gateway")).ReadVariables(
            gw.ReadVariablesRequest(gateway_id=gateway_id, variable_ids=[variable_id]),
            timeout=10,
        )
        assert len(reply.values) == 1
        return MessageToDict(
            reply.values[0],
            preserving_proto_field_name=True,
            always_print_fields_with_no_presence=True,
        )
    finally:
        await runtime.close()


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
    async with grpc.aio.secure_channel(
        harness.endpoints["data-generator"], _client_credentials(harness, "cli")
    ) as channel:
        return await dg_rpc.DataGeneratorServiceStub(channel).GetInputExecution(
            dg.GetInputExecutionRequest(generation_id=generation_id, request_id=request_id),
            timeout=10,
        )


def _last_cli_returncode(harness: FullChainHarness) -> int:
    rows = [line for line in harness.cli.log.read_text(encoding="utf-8").splitlines() if line]
    assert rows
    return int(json.loads(rows[-1])["returncode"])


def _certificate_material(kind: str, commander_pki: Path) -> tuple[bytes, bytes, dict[str, str]]:
    """过期 leaf 由 Commander 现役 CA 签发，其余用例外部 CA/非法 PEM。"""
    now = datetime.now(UTC)
    if kind == "expired":
        ca_certificate = x509.load_pem_x509_certificate((commander_pki / "ca.crt").read_bytes())
        loaded_key = serialization.load_pem_private_key(
            (commander_pki / "ca.key").read_bytes(), password=None
        )
        assert isinstance(loaded_key, ec.EllipticCurvePrivateKey)
        ca_key = loaded_key
    else:
        ca_key = ec.generate_private_key(ec.SECP256R1())
        ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"r4e-{kind}-ca")])
        ca_certificate = (
            x509.CertificateBuilder()
            .subject_name(ca_name)
            .issuer_name(ca_name)
            .public_key(ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=3))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(ca_key, hashes.SHA256())
        )
    key = ec.generate_private_key(ec.SECP256R1())
    not_before = now - timedelta(days=2) if kind == "expired" else now - timedelta(minutes=5)
    not_after = now - timedelta(days=1) if kind == "expired" else now + timedelta(hours=1)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "client-ops")]))
        .issuer_name(ca_certificate.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(
            x509.SubjectAlternativeName([x509.RFC822Name("ops@windhub.test")]), critical=False
        )
        .add_extension(
            x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.CLIENT_AUTH]), critical=True
        )
        .sign(ca_key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_pem = (
        b"-----BEGIN CERTIFICATE-----\nnot-a-certificate\n-----END CERTIFICATE-----\n"
        if kind == "invalid-pem"
        else certificate.public_bytes(serialization.Encoding.PEM)
    )
    return (
        certificate_pem,
        key_pem,
        {
            "not_before": not_before.isoformat(),
            "not_after": not_after.isoformat(),
            "issuer": ca_certificate.subject.rfc4514_string(),
        },
    )


@pytest.mark.parametrize("kind", ["expired", "invalid-pem", "untrusted-ca"])
async def test_r4e_mtls_rejects_bad_client_handshake_without_write(
    full_chain: FullChainHarness, kind: str
) -> None:
    commander_pki = full_chain.plant / "commander-pki"
    certificate, key, certificate_evidence = _certificate_material(kind, commander_pki)
    request_id = f"r4e-expired-{uuid4()}"
    if kind == "expired":
        service = "commander"
        root_ca = commander_pki / "ca.crt"
        target_rpc = "CommanderService.SubmitCommand"
        device = await full_chain.cli_call(full_chain.cli_rt, "commander", "device", "PPC_SIM01")
        revision = str(device["data"]["device"]["revision"])
    else:
        service = "gateway"
        root_ca = full_chain.pki["ca"]
        target_rpc = "IndustrialGatewayService.UpdateVariables"
        before = await _read_gateway(full_chain, "gw-gw02", "a02_dm_pitchangle1")
    actual_endpoint = full_chain.endpoints[service]
    credentials = grpc.ssl_channel_credentials(
        root_certificates=root_ca.read_bytes(),
        private_key=key,
        certificate_chain=certificate,
    )
    async with grpc.aio.secure_channel(actual_endpoint, credentials) as channel:
        with pytest.raises(grpc.aio.AioRpcError) as exc_info:
            if kind == "expired":
                await cmd_rpc.CommanderServiceStub(channel).SubmitCommand(
                    cmd.SubmitCommandRequest(
                        device_id="PPC_SIM01",
                        point_id="agc_enable",
                        value=common.ScalarValue(uint64_value=1),
                        expected_revision=revision,
                        request_id=request_id,
                    ),
                    timeout=10,
                )
            else:
                await gw_rpc.IndustrialGatewayServiceStub(channel).UpdateVariables(
                    gw.UpdateVariablesRequest(
                        gateway_id="gw-gw02",
                        lease_token="handshake-must-fail-before-lease-validation",
                        values=[
                            gw.VariableUpdate(
                                variable_id="a02_dm_pitchangle1",
                                value=common.ScalarValue(double_value=99.0),
                            )
                        ],
                    ),
                    timeout=10,
                )
    assert exc_info.value.code() in {
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.UNAVAILABLE,
    }
    if kind == "expired":
        readback = await full_chain.cli_call(
            full_chain.cli_rt, "commander", "request-result", request_id, check=False
        )
        assert readback["status"] == "FAILURE"
        assert readback["error_code"] == "NOT_FOUND"
        after: Any = {"request_result": readback["error_code"]}
        before = {"request_result": "NOT_SUBMITTED"}
        no_side_effect = "commander_request_absent"
    else:
        after = await _read_gateway(full_chain, "gw-gw02", "a02_dm_pitchangle1")
        assert after == before
        readback = after
        no_side_effect = "gateway_value_unchanged"
    full_chain.record(
        "R4-E-MTLS-HANDSHAKE",
        kind=kind,
        service=service,
        actual_endpoint=actual_endpoint,
        trusted_root_ca=str(root_ca),
        target_rpc=target_rpc,
        grpc_status=exc_info.value.code().name,
        certificate=certificate_evidence,
        request_id=request_id if kind == "expired" else None,
        before=before,
        after=after,
        readback=readback,
        assertions={"handshake_rejected": True, no_side_effect: True},
    )


async def test_r4e_commander_identity_cannot_write_gateway_or_generator(
    full_chain: FullChainHarness,
) -> None:
    generation_id: str | None = None
    request_id = f"r4e-unauthorized-{uuid4()}"
    try:
        generation_id = await _start_generation(full_chain)
        gateway_before = await _read_gateway(full_chain, "gw-wtg_sim01", "wind_speed")
        credentials = _client_credentials(full_chain, "commander")
        async with grpc.aio.secure_channel(
            full_chain.endpoints["gateway"], credentials
        ) as gateway_channel:
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

        async with grpc.aio.secure_channel(
            full_chain.endpoints["data-generator"], credentials
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
        assert generator_error.value.code() is grpc.StatusCode.PERMISSION_DENIED

        gateway_after = await _read_gateway(full_chain, "gw-wtg_sim01", "wind_speed")
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
        assert rejected["data"]["accepted"] is False
        assert rejected["data"]["error_code"] == "INPUT_REJECTED"
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
            rejected_error_code=rejected["data"]["error_code"],
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
    async with grpc.aio.secure_channel(
        full_chain.endpoints["gateway"], _client_credentials(full_chain, "cli")
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
    assert after == before
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
    pki = full_chain.plant / "commander-pki"
    credentials = grpc.ssl_channel_credentials(
        root_certificates=(pki / "ca.crt").read_bytes(),
        private_key=(pki / "ops.key").read_bytes(),
        certificate_chain=(pki / "ops.crt").read_bytes(),
    )
    async with grpc.aio.secure_channel(full_chain.endpoints["commander"], credentials) as channel:
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
