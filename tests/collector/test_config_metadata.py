"""GetPointMetadata 与配置版本的一致性（v2，真实 gRPC 链路）。

替代 v1 的 prepare/activate/abort + 元数据服务测试：

- 元数据是只读查询：不触发任何现场 I/O（设备保持 STOPPED，连接计数为 0）；
- 请求顺序（含重复点）保持；
- active_revision 等于 devices 文档当前 revision，ReplaceConfig 成功后随之移动；
- 非法候选在落盘前拒绝，元数据仍指向旧版本。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import grpc
import pytest
import yaml

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

ADMIN_METADATA = (("x-windhub-admin-token", "metadata-token"),)


@pytest.fixture
async def versioned_stack(tmp_path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[dict]:
    tree: ConfigTree = make_config_tree(tmp_path)
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ADMIN_TOKEN", "metadata-token")
    monkeypatch.setenv("WIND_HUB_COLLECTOR_ID", "metadata-version-collector")
    app = assemble_collector(
        tree.service,
        tree.instances,
        tree.templates,
        collector_id="metadata-version-collector",
    )
    await app.start()
    server = grpc.aio.server()
    pb_grpc.add_CollectorRuntimeServiceServicer_to_server(CollectorRuntimeServiceV2(app), server)
    pb_grpc.add_CollectorTaskServiceServicer_to_server(CollectorTaskServiceV2(app), server)
    pb_grpc.add_CollectorConfigServiceServicer_to_server(CollectorConfigServiceV2(app), server)
    pb_grpc.add_CollectorMetricsServiceServicer_to_server(CollectorMetricsServiceV2(app), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        yield {
            "app": app,
            "tree": tree,
            "runtime": pb_grpc.CollectorRuntimeServiceStub(channel),
            "config": pb_grpc.CollectorConfigServiceStub(channel),
            "metrics": pb_grpc.CollectorMetricsServiceStub(channel),
        }
    finally:
        await channel.close()
        await server.stop(1)
        await app.stop()


def _devices_selector() -> pb.ConfigSelector:
    return pb.ConfigSelector(scope=pb.INSTANCE, name="devices")


async def _metadata(stub, device_id: str = "WT01", point_ids=()):
    return await stub.GetPointMetadata(
        pb.GetPointMetadataRequest(device_id=device_id, point_ids=list(point_ids))
    )


async def test_metadata_is_read_only_no_field_io(versioned_stack: dict) -> None:
    metrics_stub = versioned_stack["metrics"]

    async def _connect_failures() -> float:
        response = await metrics_stub.GetMetrics(pb.GetMetricsRequest())
        counters = {row.name: row.value for row in response.metrics}
        return counters.get("connect_failures", 0.0)

    # 单调累计计数：以查询前一刻为基线，只读查询本身不得带来新增连接尝试。
    baseline = await _connect_failures()
    before = await _metadata(versioned_stack["runtime"], point_ids=["active_power"])
    assert before.points[0].point_id == "active_power"
    # 重复若干次只读查询。
    for _ in range(3):
        again = await _metadata(versioned_stack["runtime"], point_ids=["active_power"])
        assert again == before
    # 只读查询不得触发任何新连接尝试。
    response = await metrics_stub.GetMetrics(pb.GetMetricsRequest())
    counters = {row.name: row.value for row in response.metrics}
    assert counters.get("connect_failures", 0.0) == baseline


async def test_request_order_and_duplicates_preserved(versioned_stack: dict) -> None:
    response = await _metadata(
        versioned_stack["runtime"],
        point_ids=["active_power", "nope", "active_power"],
    )
    assert [row.point_id for row in response.points] == ["active_power", "active_power"]


async def _devices_revision(config_stub) -> str:
    response = await config_stub.GetConfig(
        pb.GetConfigRequest(selector=_devices_selector()), metadata=ADMIN_METADATA
    )
    return response.revision


async def test_revision_moves_after_successful_replace(versioned_stack: dict) -> None:
    config_stub = versioned_stack["config"]
    tree: ConfigTree = versioned_stack["tree"]
    before = await _metadata(versioned_stack["runtime"])
    revision = await _devices_revision(config_stub)
    assert revision == config_hash(read_yaml_mapping(tree.devices_yaml))

    on_disk = read_yaml_mapping(tree.devices_yaml)
    candidate = dict(on_disk)
    candidate["devices"] = {k: dict(v) for k, v in on_disk["devices"].items()}
    candidate["devices"]["WT01"]["endpoint"] = dict(
        candidate["devices"]["WT01"]["endpoint"], port=1503
    )
    result = await config_stub.ReplaceConfig(
        pb.ReplaceConfigRequest(
            selector=_devices_selector(),
            expected_revision=revision,
            request_id="meta-rev-1",
            yaml_content=yaml.safe_dump(candidate),
        ),
        metadata=ADMIN_METADATA,
    )
    # 设备保持 STOPPED：仅重配置会话，不发起采集；替换必须收敛成功。
    assert result.success, [issue.message for issue in result.errors]
    assert result.stored_revision == config_hash(candidate)
    after = await _metadata(versioned_stack["runtime"])
    # 元数据的 active_revision 是全配置摘要：devices 变化后必须前移到新摘要。
    assert after.active_revision != before.active_revision
    assert after.active_revision == versioned_stack["app"].config.active_revision


async def test_invalid_candidate_leaves_metadata_revision_untouched(
    versioned_stack: dict,
) -> None:
    config_stub = versioned_stack["config"]
    before = await _metadata(versioned_stack["runtime"])
    revision = await _devices_revision(config_stub)
    on_disk = read_yaml_mapping(versioned_stack["tree"].devices_yaml)
    candidate = dict(on_disk)
    candidate["devices"] = {k: dict(v) for k, v in on_disk["devices"].items()}
    candidate["devices"]["WT01"]["bogus_field"] = 1
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await config_stub.ReplaceConfig(
            pb.ReplaceConfigRequest(
                selector=_devices_selector(),
                expected_revision=revision,
                request_id="meta-rev-bad",
                yaml_content=yaml.safe_dump(candidate),
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT
    after = await _metadata(versioned_stack["runtime"])
    assert after == before
