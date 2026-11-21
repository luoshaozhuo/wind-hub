"""E2E-04: request idempotency and real Commander Bearer authorization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import grpc
import pytest
from google.protobuf.timestamp_pb2 import Timestamp

from contracts import commander_pb2 as cmd
from contracts import commander_pb2_grpc as cmd_rpc
from contracts import common_pb2 as common
from tests.support.bearer import bearer_channel

pytestmark = [pytest.mark.system, pytest.mark.real_service, pytest.mark.docker, pytest.mark.slow]


async def test_e2e_04_request_replay_and_unauthorized_control(full_chain: Any) -> None:
    harness = full_chain
    generation_id: str | None = None
    try:
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

        device = await harness.cli_call(harness.cli_rt, "commander", "device", "PPC_SIM01")
        revision = str(device["data"]["device"]["revision"])
        request_id = f"e2e-idempotent-{uuid4()}"
        expires_at = (datetime.now(UTC) + timedelta(minutes=2)).isoformat()
        args = (
            "commander",
            "write",
            "PPC_SIM01",
            "agc_enable",
            "1",
            "--expected-revision",
            revision,
            "--expires-at",
            expires_at,
            "--request-id",
            request_id,
            "--yes",
        )
        first = await harness.cli_call(harness.cli_rt, *args)
        command_id = str(first["data"]["command_id"])
        completed = await harness.poll(
            harness.cli_rt,
            "commander",
            "result",
            command_id,
            predicate=lambda item: item.get("state") in {"SUCCEEDED", "FAILED", "UNKNOWN"},
            timeout=60,
        )
        assert completed["state"] == "SUCCEEDED"
        assert completed["outcome"] == "CONFIRMED_SUCCESS"
        assert completed["io_settled"] is True

        replay = await harness.cli_call(harness.cli_rt, *args)
        assert replay["data"]["command_id"] == command_id
        assert replay["data"]["request_id"] == request_id

        conflict_args = list(args)
        conflict_args[4] = "0"
        conflict = await harness.cli_call(
            harness.cli_rt,
            *conflict_args,
            check=False,
        )
        assert conflict["status"] == "FAILURE"
        assert conflict["error_code"] == "ALREADY_EXISTS"

        # The viewer is authenticated for query only and cannot submit control.
        deadline = Timestamp()
        deadline.FromDatetime(datetime.now(UTC) + timedelta(minutes=2))
        unauthorized_id = f"e2e-unauthorized-{uuid4()}"
        async with bearer_channel(harness.endpoints["commander"], "viewer") as channel:
            stub = cmd_rpc.CommanderServiceStub(channel)
            with pytest.raises(grpc.aio.AioRpcError) as exc_info:
                await stub.SubmitCommand(
                    cmd.SubmitCommandRequest(
                        device_id="PPC_SIM01",
                        point_id="agc_enable",
                        value=common.ScalarValue(uint64_value=0),
                        expires_at=deadline,
                        expected_revision=revision,
                        request_id=unauthorized_id,
                    ),
                    timeout=10,
                )
            assert exc_info.value.code() is grpc.StatusCode.PERMISSION_DENIED

        lookup = await harness.cli_call(
            harness.cli_rt,
            "commander",
            "request-result",
            unauthorized_id,
            check=False,
        )
        assert lookup["status"] == "FAILURE"
        assert lookup["error_code"] == "NOT_FOUND"
        harness.record(
            "E2E-04-SECURITY",
            generation_id=generation_id,
            request_id=request_id,
            command_id=command_id,
            replay_command_id=replay["data"]["command_id"],
            conflict=conflict["error_code"],
            unauthorized_status=exc_info.value.code().name,
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
