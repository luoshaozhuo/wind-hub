"""Round2 契约缺陷修复后的回归钉死（原 strict xfail，f341118f/caa46f6c 转绿）。

- D-CONFIG-1：reload 收敛抛异常时响应仍携带受影响设备明细。
- D-CONFIG-2：UpdateConfig 的 SET 整型补丁不被 protobuf double 编码吞掉。
- D-CONFIG-3：模板发布授权失败映射 PERMISSION_DENIED 而非 UNKNOWN。
"""

from __future__ import annotations

import os

import grpc
import pytest
import yaml
from google.protobuf import struct_pb2

from contracts import collector_v2_pb2 as pb
from core.infrastructure.config.yaml import read_yaml_mapping

pytestmark = pytest.mark.component

ADMIN_METADATA = (("x-windhub-admin-token", "acceptance-token"),)


def _devices_selector() -> pb.ConfigSelector:
    return pb.ConfigSelector(scope=pb.INSTANCE, name="devices")


async def test_d_config_1_reload_exception_keeps_affected_devices(grpc_stack: dict) -> None:
    """D-CONFIG-1：reload 内异常时响应仍应携带受影响设备与失败记账。"""
    stub = grpc_stack["config"]
    tree = grpc_stack["tree"]
    on_disk = read_yaml_mapping(tree.devices_yaml)
    from core.infrastructure.config.versioned_templates import config_hash

    revision = config_hash(on_disk)
    candidate = dict(on_disk)
    candidate["devices"] = {
        device_id: dict(item) for device_id, item in candidate["devices"].items()
    }
    candidate["devices"]["WT01"]["endpoint"] = dict(
        candidate["devices"]["WT01"]["endpoint"], port=1701
    )
    response = await stub.ReplaceConfig(
        pb.ReplaceConfigRequest(
            selector=_devices_selector(),
            expected_revision=revision,
            request_id="d1",
            yaml_content=yaml.safe_dump(candidate),
        ),
        metadata=ADMIN_METADATA,
    )
    assert {item.device_id for item in response.affected_devices} == {"WT01"}


async def test_d_config_2_set_integer_patch_succeeds(grpc_stack: dict) -> None:
    """D-CONFIG-2：SET 整数字段（endpoint.port）应通过严格 int 校验。"""
    stub = grpc_stack["config"]
    tree = grpc_stack["tree"]
    from core.infrastructure.config.versioned_templates import config_hash

    revision = config_hash(read_yaml_mapping(tree.devices_yaml))
    await stub.UpdateConfig(
        pb.UpdateConfigRequest(
            selector=_devices_selector(),
            expected_revision=revision,
            request_id="d2",
            updates=[
                pb.ConfigPatch(
                    path="/devices/WT01/endpoint/port",
                    operation=pb.SET,
                    value=struct_pb2.Value(number_value=1702),
                )
            ],
        ),
        metadata=ADMIN_METADATA,
    )
    assert read_yaml_mapping(tree.devices_yaml)["devices"]["WT01"]["endpoint"]["port"] == 1702


async def test_d_config_3_template_publish_denied_has_correct_status(grpc_stack: dict) -> None:
    """D-CONFIG-3：未授权模板发布必须映射 PERMISSION_DENIED。"""
    stub = grpc_stack["config"]
    tree = grpc_stack["tree"]
    source = read_yaml_mapping(tree.templates / "protocols/modbus_tcp/1.0.0.yaml")
    source = dict(source)
    source["template_version"] = "3.0.0"
    os.environ.pop("WIND_HUB_COLLECTOR_TEMPLATE_PUBLISH", None)
    with pytest.raises(grpc.aio.AioRpcError) as exc:
        await stub.ReplaceConfig(
            pb.ReplaceConfigRequest(
                selector=pb.ConfigSelector(
                    scope=pb.TEMPLATE, name="protocols/modbus_tcp", version="3.0.0"
                ),
                expected_revision="CREATE",
                request_id="d3",
                yaml_content=yaml.safe_dump(source),
            ),
            metadata=ADMIN_METADATA,
        )
    assert exc.value.code() == grpc.StatusCode.PERMISSION_DENIED
