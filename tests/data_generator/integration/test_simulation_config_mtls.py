"""Paired factory simulation configs with environment-scoped Bearer credentials."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from contracts import common_pb2 as common
from data_generator.infrastructure.adapters.inbound.diagnostics import Diagnostics
from data_generator.infrastructure.config.service_settings import load_settings
from industrial_gateway.assembly import assemble_gateway
from industrial_gateway.infrastructure.adapters.inbound.grpc.data_server import (
    create_gateway_grpc_server,
)
from industrial_gateway.infrastructure.config.resolution import load_gateway
from industrial_gateway.infrastructure.config.snapshots import ConfigSnapshots
from tests.support.gateway import reserve_port

pytestmark = [pytest.mark.integration, pytest.mark.real_service]

PROJECT_ROOT = Path(__file__).parents[3]


def _provision_paired_configs(root: Path) -> tuple[Path, Path]:
    """Copy the paired Bearer configuration and allocate its real listener."""
    shutil.copytree(PROJECT_ROOT / "configs", root / "configs")
    generator_path = root / "configs/services/data-generator-sim.example.yaml"
    gateway_path = root / "configs/services/industrial_gateway-sim.example.yaml"
    port = reserve_port()
    for path in (generator_path, gateway_path):
        text = path.read_text(encoding="utf-8")
        text = text.replace("50073", str(port))
        path.write_text(text, encoding="utf-8")
    return generator_path, gateway_path


async def test_paired_simulation_configs_check_and_diagnose_over_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, token in (
        (
            "WIND_HUB_GENERATOR_GATEWAY_TOKEN",
            "paired-generator-token-012345678901234567890123456789",
        ),
        (
            "WIND_HUB_GATEWAY_COMMANDER_TOKEN",
            "paired-commander-token-01234567890123456789012345678",
        ),
        (
            "WIND_HUB_GATEWAY_OPERATOR_TOKEN",
            "paired-operator-token-0123456789012345678901234567890",
        ),
        (
            "WIND_HUB_COMMANDER_GATEWAY_TOKEN",
            "paired-gateway-control-token-012345678901234567890123456",
        ),
        (
            "WIND_HUB_GENERATOR_INPUT_TOKEN",
            "paired-generator-input-token-012345678901234567890123456",
        ),
    ):
        monkeypatch.setenv(name, token)
    generator_path, gateway_path = _provision_paired_configs(tmp_path)
    checked = subprocess.run(
        [sys.executable, "-m", "data_generator.main", "--config", str(generator_path), "--check"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert checked.returncode == 0, checked.stderr

    config = load_gateway(gateway_path, project_root=tmp_path)
    runtime = assemble_gateway(config)
    snapshots = ConfigSnapshots(runtime, gateway_path, tmp_path)
    server = create_gateway_grpc_server(runtime, snapshots)
    await runtime.start()
    await server.start()
    try:
        response = await Diagnostics(load_settings(generator_path)).run(
            common.DiagnoseRequest(
                target_id="wind-plant-physical", stages=["grpc"], timeout_ms=3000
            )
        )
        assert response.overall_status == "PASS", response.stages[0].message
        evidence = {item.name: item.value for item in response.stages[0].evidence}
        assert len(evidence) == 5
        assert all(int(count) > 0 for count in evidence.values())
    finally:
        await server.stop(grace=0)
        await runtime.stop()
