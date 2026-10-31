"""Collector v2 设备级 START/STOP 控制面的护栏与状态持久化。

v1 的 Task Placement 所有权栅栏已随 Commander 解耦移除；设备 Task 的生命周期
契约改为：

- 管理员令牌强制（无令牌/错令牌 → PERMISSION_DENIED）；
- 乐观版本（expected_revision 过期 → ABORTED）；
- device_id/expected_revision/request_id 必填，未知设备 → NOT_FOUND；
- START/STOP 意图持久化到 devices.yaml
  （overrides.task.desired_state），冷启动与 Watchdog 按持久化意图恢复，
  这是「手动 STOP 不被复活」的契约基础。

用真实 grpc.aio in-process server + 真实 assemble_collector 并 start 运行时
（默认意图 STOP，不发起采集；START 用例的 Modbus endpoint 不可达，用于验证
持久化不依赖即时收敛）。Watchdog 恢复链路在第三轮真实运行时测试覆盖。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import grpc
import pytest

from collector.assembly import assemble_collector
from collector.infrastructure.grpc.v2_server import (
    CollectorConfigServiceV2,
    CollectorMetricsServiceV2,
    CollectorRuntimeServiceV2,
    CollectorTaskServiceV2,
)
from contracts import collector_v2_pb2 as pb
from contracts import collector_v2_pb2_grpc as pb_grpc
from core.infrastructure.config.versioned_templates import config_hash
from core.infrastructure.config.yaml import read_yaml_mapping
from tests.collector.config_v2.config_tree import ConfigTree, make_config_tree

pytestmark = pytest.mark.component

ADMIN_TOKEN = "fencing-token"
ADMIN_METADATA = (("x-windhub-admin-token", ADMIN_TOKEN),)


@pytest.fixture
async def task_stack(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict]:
    tree: ConfigTree = make_config_tree(tmp_path)
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ID", "fencing-collector")
    app = assemble_collector(
        tree.service,
        tree.instances,
        tree.templates,
        collector_id="fencing-collector",
    )
    server = grpc.aio.server()
    pb_grpc.add_CollectorRuntimeServiceServicer_to_server(CollectorRuntimeServiceV2(app), server)
    pb_grpc.add_CollectorTaskServiceServicer_to_server(CollectorTaskServiceV2(app), server)
    pb_grpc.add_CollectorConfigServiceServicer_to_server(CollectorConfigServiceV2(app), server)
    pb_grpc.add_CollectorMetricsServiceServicer_to_server(CollectorMetricsServiceV2(app), server)
    await app.start()
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield {
            "app": app,
            "tree": tree,
            "tasks": pb_grpc.CollectorTaskServiceStub(channel),
            "config": pb_grpc.CollectorConfigServiceStub(channel),
        }
    finally:
        await channel.close()
        await server.stop(1)
        await app.stop()


async def _devices_revision(stub) -> str:
    response = await stub.GetConfig(
        pb.GetConfigRequest(selector=pb.ConfigSelector(scope=pb.INSTANCE, name="devices")),
        metadata=ADMIN_METADATA,
    )
    return response.revision


async def test_start_requires_admin_token(task_stack: dict) -> None:
    tasks = task_stack["tasks"]
    request = pb.TaskChangeRequest(device_id="WT01", expected_revision="x", request_id="r1")
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await tasks.StartTask(request)
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await tasks.StartTask(request, metadata=(("x-windhub-admin-token", "wrong"),))
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED


async def test_start_missing_required_fields_rejected(task_stack: dict) -> None:
    tasks = task_stack["tasks"]
    revision = await _devices_revision(task_stack["config"])
    for kwargs in (
        {"device_id": "", "expected_revision": revision, "request_id": "r1"},
        {"device_id": "WT01", "expected_revision": "", "request_id": "r1"},
        {"device_id": "WT01", "expected_revision": revision, "request_id": ""},
    ):
        with pytest.raises(grpc.aio.AioRpcError) as exc:
            await tasks.StartTask(pb.TaskChangeRequest(**kwargs), metadata=ADMIN_METADATA)
        assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_unknown_device_is_not_found(task_stack: dict) -> None:
    tasks = task_stack["tasks"]
    revision = await _devices_revision(task_stack["config"])
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await tasks.StartTask(
            pb.TaskChangeRequest(device_id="NOPE", expected_revision=revision, request_id="r1"),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND


async def test_stale_revision_aborts_before_persistence(task_stack: dict) -> None:
    tasks = task_stack["tasks"]
    tree: ConfigTree = task_stack["tree"]
    raw_before = tree.devices_yaml.read_bytes()
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await tasks.StartTask(
            pb.TaskChangeRequest(device_id="WT01", expected_revision="stale", request_id="r1"),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.ABORTED
    # 乐观版本失败必须 fail-closed：意图不落盘。
    assert tree.devices_yaml.read_bytes() == raw_before


async def test_start_persists_desired_state_even_when_device_unreachable(
    task_stack: dict,
) -> None:
    # 发货配置里的 WT01 指向不可达的 Modbus endpoint；START 的持久化契约
    # 不依赖即时收敛：意图必须落盘并返回新 revision（冷启动/看门狗据此恢复，
    # 收敛失败后的实际状态由第三轮运行时测试覆盖）。
    tasks = task_stack["tasks"]
    tree: ConfigTree = task_stack["tree"]
    revision = config_hash(read_yaml_mapping(tree.devices_yaml))
    response = await tasks.StartTask(
        pb.TaskChangeRequest(device_id="WT01", expected_revision=revision, request_id="r1"),
        metadata=ADMIN_METADATA,
    )
    assert response.device_id == "WT01"
    assert response.desired_state == pb.START
    persisted = read_yaml_mapping(tree.devices_yaml)
    assert persisted["devices"]["WT01"]["overrides"]["task"]["desired_state"] == "START"
    assert response.stored_revision == config_hash(persisted)


async def test_start_then_stop_advances_revision_and_persists_stop(
    task_stack: dict,
) -> None:
    tasks = task_stack["tasks"]
    revision = await _devices_revision(task_stack["config"])
    started = await tasks.StartTask(
        pb.TaskChangeRequest(device_id="WT01", expected_revision=revision, request_id="r1"),
        metadata=ADMIN_METADATA,
    )
    stopped = await tasks.StopTask(
        pb.TaskChangeRequest(
            device_id="WT01", expected_revision=started.stored_revision, request_id="r2"
        ),
        metadata=ADMIN_METADATA,
    )
    assert stopped.desired_state == pb.STOP
    tree: ConfigTree = task_stack["tree"]
    persisted = read_yaml_mapping(tree.devices_yaml)
    assert persisted["devices"]["WT01"]["overrides"]["task"]["desired_state"] == "STOP"
    assert stopped.stored_revision == config_hash(persisted)
