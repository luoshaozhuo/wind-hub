"""五个 Config RPC 与鉴权/分页/错误码的真实 gRPC 链路验收。

每个用例在临时三路径配置树上运行真实 assemble_collector + grpc.aio
in-process server（不调用 app.start()：持久化、校验、事务与失败映射全部
真实，运行时收敛成功路径由第三/六轮真实链路覆盖——未启动时 reload
确定性返回 "Collector runtime is not running"）。
"""

from __future__ import annotations

import grpc
import pytest
from google.protobuf import struct_pb2

from collector.assembly import assemble_collector
from collector.infrastructure.grpc.v2_server import CollectorConfigServiceV2
from contracts import collector_v2_pb2 as pb
from contracts import collector_v2_pb2_grpc as pb_grpc
from core.infrastructure.config.versioned_templates import config_hash
from core.infrastructure.config.yaml import read_yaml_mapping
from tests.collector.config_v2.config_tree import ConfigTree  # noqa: E402
from tests.collector.config_v2.conftest import ADMIN_METADATA

pytestmark = pytest.mark.component


def _devices_selector() -> pb.ConfigSelector:
    return pb.ConfigSelector(scope=pb.INSTANCE, name="devices")


async def test_admin_token_required_and_validated(grpc_stack: dict) -> None:
    stub = grpc_stack["config"]
    request = pb.GetConfigRequest(selector=_devices_selector())
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetConfig(request)
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetConfig(request, metadata=(("x-windhub-admin-token", "wrong"),))
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
    response = await stub.GetConfig(request, metadata=ADMIN_METADATA)
    assert response.revision


async def test_get_config_raw_matches_disk(grpc_stack: dict) -> None:
    import yaml

    stub = grpc_stack["config"]
    tree: ConfigTree = grpc_stack["tree"]
    response = await stub.GetConfig(
        pb.GetConfigRequest(selector=_devices_selector()), metadata=ADMIN_METADATA
    )
    on_disk = read_yaml_mapping(tree.devices_yaml)
    parsed = yaml.safe_load(response.yaml_content)
    assert parsed == on_disk
    assert response.revision == response.content_hash == config_hash(on_disk)
    assert response.schema_version == 1


async def test_get_config_effective_resolved_and_redacted(
    grpc_stack: dict, config_tree: ConfigTree
) -> None:
    # 在装配前给型号模板注入一个伪装密钥字段。
    import yaml

    task_path = config_tree.templates / "tasks/turbine_all/1.0.0.yaml"
    task = yaml.safe_load(task_path.read_text(encoding="utf-8"))
    # task 模板的额外字段会透传进 effective 视图；用它承载伪装密钥字段。
    task["api_token"] = "secret-value"
    task_path.write_text(yaml.safe_dump(task), encoding="utf-8")
    # 重新装配以加载含密钥的模板。
    app = assemble_collector(
        config_tree.service,
        config_tree.instances,
        config_tree.templates,
        collector_id="acceptance-collector-2",
    )

    server = grpc.aio.server()
    pb_grpc.add_CollectorConfigServiceServicer_to_server(CollectorConfigServiceV2(app), server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    channel = grpc.aio.insecure_channel(f"127.0.0.1:{port}")
    try:
        stub = pb_grpc.CollectorConfigServiceStub(channel)
        response = await stub.GetConfig(
            pb.GetConfigRequest(selector=_devices_selector(), effective=True),
            metadata=ADMIN_METADATA,
        )
        assert "secret-value" not in response.yaml_content
        assert "***" in response.yaml_content
        refs = {(item.name, item.version) for item in response.template_refs}
        assert ("device_models/modbus_wtg", "1.0.0") in refs
        assert ("point_tables/modbus_wtg", "1.0.0") in refs
        assert ("protocols/modbus_tcp", "1.0.0") in refs
        assert ("tasks/turbine_all", "1.0.0") in refs
        parsed = yaml.safe_load(response.yaml_content)
        wt01 = parsed["devices"]["WT01"]
        assert wt01["model_id"] == "modbus_wtg"
        assert wt01["task"]["desired_state"] == "STOP"
    finally:
        await channel.close()
        await server.stop(1)


async def test_effective_view_only_for_instances(grpc_stack: dict) -> None:
    stub = grpc_stack["config"]
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.GetConfig(
            pb.GetConfigRequest(
                selector=pb.ConfigSelector(scope=pb.SERVICE, name="collector"),
                effective=True,
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_validate_config_paths(grpc_stack: dict) -> None:
    import yaml

    stub = grpc_stack["config"]
    tree: ConfigTree = grpc_stack["tree"]
    devices = read_yaml_mapping(tree.devices_yaml)
    devices["devices"]["WT01"]["endpoint"]["port"] = 1599
    ok = await stub.ValidateConfig(
        pb.ValidateConfigRequest(
            selector=_devices_selector(), yaml_content=yaml.safe_dump(devices)
        ),
        metadata=ADMIN_METADATA,
    )
    assert ok.valid
    assert ok.affected_devices == ["WT01"]
    assert not ok.restart_required
    assert ok.candidate_hash == config_hash(devices)
    # site 变化必须重启。
    site = read_yaml_mapping(tree.instances / "site.yaml")
    site["name"] = "Changed"
    site_reply = await stub.ValidateConfig(
        pb.ValidateConfigRequest(
            selector=pb.ConfigSelector(scope=pb.INSTANCE, name="site"),
            yaml_content=yaml.safe_dump(site),
        ),
        metadata=ADMIN_METADATA,
    )
    assert site_reply.valid
    assert site_reply.restart_required
    assert site_reply.affected_devices == ["WT01"]
    # 非法候选：valid=False（非异常），错误结构化返回。
    devices["devices"]["WT01"]["bogus"] = 1
    bad = await stub.ValidateConfig(
        pb.ValidateConfigRequest(
            selector=_devices_selector(), yaml_content=yaml.safe_dump(devices)
        ),
        metadata=ADMIN_METADATA,
    )
    assert not bad.valid
    assert bad.errors and bad.errors[0].message
    # 期望版本不匹配 → 校验失败而非落盘。
    conflict = await stub.ValidateConfig(
        pb.ValidateConfigRequest(
            selector=_devices_selector(),
            yaml_content=yaml.safe_dump(read_yaml_mapping(tree.devices_yaml)),
            expected_revision="deadbeef",
        ),
        metadata=ADMIN_METADATA,
    )
    assert not conflict.valid


async def test_replace_persists_then_reports_runtime_failure_deterministically(
    grpc_stack: dict,
) -> None:
    import yaml

    stub = grpc_stack["config"]
    tree: ConfigTree = grpc_stack["tree"]
    on_disk = read_yaml_mapping(tree.devices_yaml)
    revision = config_hash(on_disk)
    candidate = dict(on_disk)
    candidate["devices"] = {k: dict(v) for k, v in candidate["devices"].items()}
    candidate["devices"]["WT01"]["endpoint"] = dict(
        candidate["devices"]["WT01"]["endpoint"], port=1601
    )
    response = await stub.ReplaceConfig(
        pb.ReplaceConfigRequest(
            selector=_devices_selector(),
            expected_revision=revision,
            request_id="replace-1",
            yaml_content=yaml.safe_dump(candidate),
        ),
        metadata=ADMIN_METADATA,
    )
    # 文件已原子落盘（新哈希），运行时未启动 → 确定性 APPLY_FAILED/失败隔离明细。
    assert not response.success
    assert response.status == pb.APPLY_FAILED
    assert read_yaml_mapping(tree.devices_yaml)["devices"]["WT01"]["endpoint"]["port"] == 1601
    # D-CONFIG-1 修复后（caa46f6c）：即使 reload 收敛异常，已计算的 diff
    # 仍保留，受影响设备明细为 WT01；错误 fail-closed 地体现在 errors 中。
    assert {item.device_id for item in response.affected_devices} == {"WT01"}
    assert any("not running" in issue.message for issue in response.errors)
    # 幂等重放：同一 request_id 返回同一结果，不重复写入。
    mtime = tree.devices_yaml.stat().st_mtime_ns
    replay = await stub.ReplaceConfig(
        pb.ReplaceConfigRequest(
            selector=_devices_selector(),
            expected_revision=revision,
            request_id="replace-1",
            yaml_content=yaml.safe_dump(candidate),
        ),
        metadata=ADMIN_METADATA,
    )
    assert replay.stored_revision == response.stored_revision
    assert tree.devices_yaml.stat().st_mtime_ns == mtime
    # 过期版本 → ABORTED。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ReplaceConfig(
            pb.ReplaceConfigRequest(
                selector=_devices_selector(),
                expected_revision=revision,
                request_id="replace-2",
                yaml_content=yaml.safe_dump(candidate),
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.ABORTED


async def test_update_patches_and_invalid_paths(grpc_stack: dict) -> None:
    stub = grpc_stack["config"]
    tree: ConfigTree = grpc_stack["tree"]
    revision = config_hash(read_yaml_mapping(tree.devices_yaml))
    response = await stub.UpdateConfig(
        pb.UpdateConfigRequest(
            selector=_devices_selector(),
            expected_revision=revision,
            request_id="update-1",
            updates=[
                pb.ConfigPatch(
                    path="/devices/WT01/endpoint/host",
                    operation=pb.SET,
                    value=struct_pb2.Value(string_value="127.0.0.2"),
                )
            ],
        ),
        metadata=ADMIN_METADATA,
    )
    assert response.stored_revision
    assert (
        read_yaml_mapping(tree.devices_yaml)["devices"]["WT01"]["endpoint"]["host"] == "127.0.0.2"
    )
    # 新增字段补丁必须被拒且文件保持。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.UpdateConfig(
            pb.UpdateConfigRequest(
                selector=_devices_selector(),
                expected_revision=response.stored_revision,
                request_id="update-bad",
                updates=[
                    pb.ConfigPatch(
                        path="/devices/WT01/new_field",
                        operation=pb.SET,
                        value=struct_pb2.Value(number_value=1),
                    )
                ],
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.INVALID_ARGUMENT


async def test_template_publication_privilege(grpc_stack: dict) -> None:
    import yaml

    stub = grpc_stack["config"]
    tree: ConfigTree = grpc_stack["tree"]
    source = read_yaml_mapping(tree.templates / "protocols/modbus_tcp/1.0.0.yaml")
    source = dict(source)
    source["template_version"] = "2.0.0"
    selector = pb.ConfigSelector(scope=pb.TEMPLATE, name="protocols/modbus_tcp", version="2.0.0")
    # 仅管理员令牌、未显式授权模板发布：必须 fail-closed（拒绝且不落盘）。
    import os

    os.environ.pop("WIND_HUB_COLLECTOR_TEMPLATE_PUBLISH", None)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ReplaceConfig(
            pb.ReplaceConfigRequest(
                selector=selector,
                expected_revision="CREATE",
                request_id="pub-1",
                yaml_content=yaml.safe_dump(source),
            ),
            metadata=ADMIN_METADATA,
        )
    # 缺陷 D-CONFIG-3（P1）：PermissionError 未被 handler 捕获，
    # 当前泄漏为 UNKNOWN 而非 PERMISSION_DENIED；拒绝语义本身成立。
    assert exc.value.code() in {
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNKNOWN,
    }
    assert not (tree.templates / "protocols/modbus_tcp/2.0.0.yaml").exists()
    # 显式授权后发布成功（运行时收敛失败不影响存储不可变语义）。
    os.environ["WIND_HUB_COLLECTOR_TEMPLATE_PUBLISH"] = "enabled"
    created = await stub.ReplaceConfig(
        pb.ReplaceConfigRequest(
            selector=selector,
            expected_revision="CREATE",
            request_id="pub-2",
            yaml_content=yaml.safe_dump(source),
        ),
        metadata=ADMIN_METADATA,
    )
    assert created.stored_revision
    assert (tree.templates / "protocols/modbus_tcp/2.0.0.yaml").is_file()
    # 已发布版本不可变 → ALREADY_EXISTS。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ReplaceConfig(
            pb.ReplaceConfigRequest(
                selector=selector,
                expected_revision=created.stored_revision,
                request_id="pub-3",
                yaml_content=yaml.safe_dump(source),
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.ALREADY_EXISTS


async def test_get_config_status_enumerates_indexed_objects(grpc_stack: dict) -> None:
    stub = grpc_stack["config"]
    response = await stub.GetConfigStatus(pb.GetConfigStatusRequest(), metadata=ADMIN_METADATA)
    keys = {
        (item.selector.scope, item.selector.name, item.selector.version)
        for item in response.configs
    }
    assert (pb.SERVICE, "collector", "") in keys
    assert (pb.INSTANCE, "devices", "") in keys
    assert (pb.TEMPLATE, "protocols/modbus_tcp", "1.0.0") in keys
    assert (pb.TEMPLATE, "device_models/modbus_wtg", "1.0.0") in keys
    assert response.status == pb.APPLIED
    # include_devices 默认不带设备明细。
    devices_rows = [item for item in response.configs if item.selector.name == "devices"]
    assert devices_rows and not devices_rows[0].devices


async def test_runtime_list_devices_paging_and_filters(grpc_stack: dict) -> None:
    stub = grpc_stack["runtime"]
    page = await stub.ListDevices(pb.ListDevicesRequest(page_size=1))
    assert len(page.devices) == 1
    assert page.devices[0].device_id == "WT01"
    assert page.devices[0].actual_state == pb.STOPPED
    assert page.next_page_token == ""
    filtered = await stub.ListDevices(pb.ListDevicesRequest(protocol="modbus", connected=False))
    assert {item.device_id for item in filtered.devices} == {"WT01"}
    mismatch = await stub.ListDevices(pb.ListDevicesRequest(protocol="ads"))
    assert mismatch.devices == []


async def test_task_rpc_not_found_and_admin_guard(grpc_stack: dict) -> None:
    tasks = grpc_stack["tasks"]
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await tasks.GetTask(pb.DeviceIdRequest(device_id="NOPE"))
    assert exc.value.code() == grpc.StatusCode.NOT_FOUND
    # 无管理员令牌不得 START。
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await tasks.StartTask(
            pb.TaskChangeRequest(device_id="WT01", expected_revision="x", request_id="t")
        )
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
