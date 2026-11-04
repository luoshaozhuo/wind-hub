"""P2-1 系统复测：StartGeneration 六类失败的稳定错误码（真实独立进程）。

- 变量不存在 / 类型不匹配 / 不可写 => TARGET_VALIDATION_FAILED
- Gateway 连接失败（预检）          => GATEWAY_UNAVAILABLE
- 租约被占                          => RESOURCE_BUSY（见 test_lease_system P1-01/02）
- 租约丢失（运行中服务端围栏）       => Generation FAILED（见 P1-08/P1-09）
"""

from __future__ import annotations

import pytest

from contracts import common_pb2 as common
from contracts import data_generator_pb2 as dg

from ..conftest import dg_stubs, reserve_port, ship_scenarios_document

pytestmark = [pytest.mark.system, pytest.mark.reliability]

VARIABLE = "GW01.pitch_angle"


async def _revision(common_stub) -> str:  # noqa: ANN001
    snapshot = await common_stub.GetConfigSnapshot(
        common.GetConfigSnapshotRequest(scope=common.SNAPSHOT_SCOPE_RUNTIME)
    )
    return str(snapshot.revision)


async def _start_error(
    start_fault_gateway,  # noqa: ANN001
    configs_root,
    dg_subprocess,  # noqa: ANN001
    catalog: dict[str, list[tuple[str, str, bool]]],
    expected: str,
) -> None:
    fault = await start_fault_gateway(catalog)
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": fault.bind},
            interval=1.0,
            mappings=[("gw-a", VARIABLE)],
        )
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        rejected = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(
                scenario_id="demo-random", expected_revision=await _revision(common_stub)
            )
        )
        assert rejected.accepted is False
        assert rejected.error_code == expected, rejected.error_code
        # 预检/申请失败不得产生任何写入。
        assert fault.service.update_calls["gw-a"] == []
    finally:
        await channel.close()


@pytest.mark.fault_injection
async def test_missing_variable_rejected_as_target_validation(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    await _start_error(
        start_fault_gateway,
        configs_root,
        dg_subprocess,
        {"gw-a": [("GW01.other_point", "float32", True)]},
        "TARGET_VALIDATION_FAILED",
    )


@pytest.mark.fault_injection
async def test_type_mismatch_rejected_as_target_validation(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    await _start_error(
        start_fault_gateway,
        configs_root,
        dg_subprocess,
        {"gw-a": [(VARIABLE, "bool", True)]},
        "TARGET_VALIDATION_FAILED",
    )


@pytest.mark.fault_injection
async def test_non_writable_rejected_as_target_validation(
    start_fault_gateway,
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    await _start_error(
        start_fault_gateway,
        configs_root,
        dg_subprocess,
        {"gw-a": [(VARIABLE, "float32", False)]},
        "TARGET_VALIDATION_FAILED",
    )


async def test_unreachable_gateway_rejected_as_unavailable(
    configs_root,
    dg_subprocess,  # noqa: ANN001
) -> None:
    closed = f"127.0.0.1:{reserve_port()}"  # 无人监听：连接被拒
    service_path = configs_root(
        scenarios=ship_scenarios_document(
            {"gw-a": closed},
            interval=1.0,
            mappings=[("gw-a", VARIABLE)],
        ),
        output_timeout=0.5,
    )
    bind, _process = await dg_subprocess(service_path)
    channel, dg_stub, common_stub = await dg_stubs(bind)
    try:
        rejected = await dg_stub.StartGeneration(
            dg.StartGenerationRequest(
                scenario_id="demo-random", expected_revision=await _revision(common_stub)
            )
        )
        assert rejected.accepted is False
        assert rejected.error_code == "GATEWAY_UNAVAILABLE", rejected.error_code
    finally:
        await channel.close()
