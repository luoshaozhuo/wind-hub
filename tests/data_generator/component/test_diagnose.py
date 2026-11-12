"""UC-05 组件级：分阶段诊断、无目标与非侵入（真实 Settings，设计 §12.1 D1~D5）。"""

from __future__ import annotations

from typing import Any

import pytest

from contracts import common_pb2 as common
from data_generator.infrastructure.config import load_settings
from data_generator.infrastructure.diagnostics import Diagnostics
from data_generator.infrastructure.snapshots import recover_transaction

from ..conftest import ship_scenarios_document

pytestmark = [pytest.mark.component, pytest.mark.reliability, pytest.mark.no_targets]


def _diagnostics(service_path) -> Diagnostics:  # noqa: ANN001
    recover_transaction(service_path.absolute().parent.parent)
    return Diagnostics(load_settings(service_path))


def _request(target: str = "", stages: Any = None, timeout_ms: int = 0) -> common.DiagnoseRequest:
    return common.DiagnoseRequest(
        target_id=target, stages=list(stages or []), timeout_ms=timeout_ms
    )


async def test_unknown_target_returns_no_diagnostic_targets(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    response = await _diagnostics(service_path).run(_request("ghost"))
    assert response.overall_status == "UNKNOWN"
    assert "NO_DIAGNOSTIC_TARGETS" in response.limitations


async def test_simulation_source_and_mapping_pass(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    response = await _diagnostics(service_path).run(_request("demo-random", ["source", "mapping"]))
    stages = {item.stage: item for item in response.stages}
    assert stages["source"].status == "PASS"
    assert stages["mapping"].status == "PASS"


async def test_write_stage_is_always_skipped_non_intrusive(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:59999"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    response = await _diagnostics(service_path).run(_request("demo-random", ["write"]))
    stage = response.stages[0]
    assert stage.status == "SKIPPED"
    assert stage.message == ""
    assert any("no authorized isolated test target" in note for note in response.limitations)


async def test_network_stage_reports_closed_port(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:9"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    response = await _diagnostics(service_path).run(_request("demo-random", ["network"], 2000))
    assert response.stages[0].status == "FAIL"
    assert response.overall_status == "FAIL"


async def test_unknown_stage_fails(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-test": "127.0.0.1:9"},
            mappings=[("gw-test", "GW01.pitch_angle")],
        ),
    )
    response = await _diagnostics(service_path).run(_request("demo-random", ["telepathy"]))
    assert response.stages[0].error_code == "UNKNOWN_STAGE"
    assert response.overall_status == "FAIL"


async def test_replay_source_stage_reads_real_csv_header(configs_root) -> None:  # noqa: ANN001
    service_path = configs_root(
        scenarios={
            "schema_version": 1,
            "file_version": "1.0.0",
            "scenarios": [
                {
                    "scenario_id": "replay-diag",
                    "version": "1.0.0",
                    "source": {
                        "kind": "replay",
                        "history_reader": "csv",
                        "file": "history/short.csv",
                        "start_time": "2026-01-01T00:00:00Z",
                        "end_time": "2026-01-01T01:00:00Z",
                        "speed": 1.0,
                        "loop": False,
                    },
                    "devices": [
                        {"device_id": "GW02", "points": [{"point_id": "a02_dm_pitchangle1"}]}
                    ],
                    "gateways": [
                        {
                            "gateway_id": "gw-test",
                            "endpoint": "127.0.0.1:59999",
                            "device_id": "GW02",
                        }
                    ],
                }
            ],
        },
        history={
            "short.csv": [
                "GW02,a02_dm_pitchangle1,2026-01-01T00:00:00Z,1.0,GOOD,r1",
                "GW02,a02_dm_pitchangle1,2026-01-01T00:00:01Z,2.0,GOOD,r2",
            ]
        },
    )
    response = await _diagnostics(service_path).run(_request("replay-diag", ["source"]))
    stage = response.stages[0]
    assert stage.status == "PASS"
    evidence = {item.name: item.value for item in stage.evidence}
    assert int(evidence["history_bytes"]) > 0
