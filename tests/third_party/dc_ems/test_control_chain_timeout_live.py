"""独立新根中的 Gateway2 连接超时故障注入（单独一轮）。"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import grpc
import pytest

from contracts import data_generator_pb2 as dg  # type: ignore[import-untyped]
from contracts import data_generator_pb2_grpc as dg_rpc
from tests.third_party.dc_ems.prepare_control_chain import (
    COMMANDER_GRPC,
    EXECUTION_GRPC,
    GENERATOR_GRPC,
)

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / "run" / "dcems-control"
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "UNKNOWN", "INTERRUPTED", "EXPIRED"}


@pytest.fixture(scope="module", autouse=True)
def require_live() -> None:
    """超时故障注入必须使用全新隔离根，单独执行。"""
    if os.environ.get("DCEMS_CONTROL_LIVE") != "1":
        pytest.skip("NOT_RUN: DCEMS_CONTROL_LIVE=1 required")
    if os.environ.get("DCEMS_CONTROL_TIMEOUT_ROUND") != "1":
        pytest.skip("NOT_RUN: dedicated fresh root with DCEMS_CONTROL_TIMEOUT_ROUND=1 required")


def _base() -> Path:
    return Path(os.environ.get("DCEMS_CONTROL_ROOT", str(BASE)))


def _cli(*args: str, check: bool = True) -> dict[str, Any]:
    finished = subprocess.run(
        [
            sys.executable,
            "-m",
            "cli.main",
            "--config",
            str(_base() / "state" / "cli.yaml"),
            *args,
        ],
        env={
            **os.environ,
            "PYTHONPATH": f"{ROOT / 'src'}:{ROOT}",
            "WIND_HUB_DEPLOYMENT_ID": "dcems-control",
            "WIND_HUB_DEPLOYMENT_VERSION": "1.0.0",
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    if check:
        assert finished.returncode == 0, (args, finished.stdout, finished.stderr)
    return cast(dict[str, Any], json.loads(finished.stdout))


def _wait_port(port: int, *, opened: bool, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                reachable = True
        except OSError:
            reachable = False
        if reachable == opened:
            return
        time.sleep(0.1)
    raise AssertionError(f"port {port} did not become {'open' if opened else 'closed'}")


def _terminal(request_id: str, timeout: float = 40.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        receipt = _cli("commander", "request-result", request_id, check=False)
        last = receipt
        data = receipt.get("data")
        if isinstance(data, dict) and data.get("state") in TERMINAL_STATES:
            return cast(dict[str, Any], data)
        time.sleep(0.2)
    raise AssertionError(f"request {request_id} did not settle: {last}")


def _generation_id() -> str:
    return (_base() / "state" / "generation-id.txt").read_text(encoding="utf-8").strip()


def _execution_state(command_id: str) -> str:
    channel = grpc.insecure_channel(f"127.0.0.1:{GENERATOR_GRPC}")
    try:
        result = dg_rpc.DataGeneratorServiceStub(channel).GetInputExecution(
            dg.GetInputExecutionRequest(generation_id=_generation_id(), request_id=command_id),
            metadata=(
                ("authorization", f"Bearer {os.environ['WIND_HUB_E2E_GATEWAY_GENERATOR_TOKEN']}"),
            ),
            timeout=10,
        )
        return str(result.state)
    except grpc.RpcError:
        return "UNAVAILABLE"
    finally:
        channel.close()


def test_gateway2_connection_timeout_is_unknown() -> None:
    """接受连接但不响应的 Gateway2 只能得到 UNKNOWN，恢复后不得自动重放。"""
    revision = str(_cli("commander", "device", "PPC_SIM01")["data"]["device"]["revision"])
    _cli("service", "stop", "gateway", "--yes")
    _wait_port(EXECUTION_GRPC, opened=False)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", EXECUTION_GRPC))
    listener.listen(8)
    stop = threading.Event()
    held: list[socket.socket] = []

    def hold() -> None:
        listener.settimeout(0.5)
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            held.append(connection)

    worker = threading.Thread(target=hold, daemon=True)
    worker.start()
    try:
        request_id = f"dcems-control-{uuid4().hex}"
        receipt = _cli(
            "commander",
            "write",
            "PPC_SIM01",
            "agc_enable",
            "0",
            "--expected-revision",
            revision,
            "--expires-at",
            (datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            "--request-id",
            request_id,
            "--yes",
        )["data"]
        assert receipt["accepted"] is True
        command = _terminal(request_id)
    finally:
        stop.set()
        worker.join(timeout=2)
        for connection in held:
            connection.close()
        listener.close()

    assert command["state"] != "SUCCEEDED", command
    assert command["commands"][0]["outcome"] != "CONFIRMED_SUCCESS", command
    command_id = str(command["commands"][0]["command_id"])
    assert _execution_state(command_id) != "APPLIED"

    _cli("service", "start", "gateway", "--yes")
    _wait_port(EXECUTION_GRPC, opened=True)
    _wait_port(COMMANDER_GRPC, opened=True)
    time.sleep(2.0)
    retained = _cli("commander", "request-result", request_id, check=False)["data"]
    assert retained["state"] == command["state"], retained
    assert len(retained["commands"]) == 1, retained
    assert _execution_state(command_id) != "APPLIED"
    evidence = {
        "request_id": request_id,
        "command_id": command_id,
        "commander_state": command["state"],
        "commander_outcome": command["commands"][0]["outcome"],
        "commander_error_code": command["commands"][0]["error_code"],
        "io_settled": command["io_settled"],
        "generator_state": _execution_state(command_id),
        "physical_plant_response_verified": False,
    }
    (_base() / "state" / "timeout-evidence.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )
