"""Commander gRPC v1 wire contract 契约测试。

Commander 承载全部即时设备读写（ReadPoint/WritePoint/Verify*），是
Server → 设备写链路唯一的 RPC 边界。契约漂移会使 Server 端命令下发以
UNIMPLEMENTED 失败，因此 service/method/message 在此固化为显式断言。
"""

from __future__ import annotations

import re

from wind_hub_core.rpc import commander_pb2 as pb

_PACKAGE = "windhub.commander.v1"

_COMMANDER_METHODS = [
    "GetStatus",
    "ListDevices",
    "PrepareConfig",
    "ActivateConfig",
    "AbortConfig",
    "ReadPoint",
    "ReadPoints",
    "WritePoint",
    "WritePoints",
    "VerifyDevice",
    "ResolvePoint",
    "VerifyPoint",
    "VerifyPoints",
]


def _service(name: str):
    try:
        return pb.DESCRIPTOR.services_by_name[name]
    except KeyError:
        raise AssertionError(f"service '{name}' missing from commander.proto") from None


def test_package_name_is_stable() -> None:
    assert pb.DESCRIPTOR.package == _PACKAGE


def test_service_set_is_stable() -> None:
    assert set(pb.DESCRIPTOR.services_by_name) == {"CommanderService"}


def test_commander_methods_are_stable() -> None:
    service = _service("CommanderService")
    assert [m.name for m in service.methods] == _COMMANDER_METHODS


def test_method_names_follow_grpc_pascal_case() -> None:
    for method in _service("CommanderService").methods:
        assert re.fullmatch(r"[A-Z][A-Za-z0-9]*", method.name), method.name


def test_write_point_request_carries_command_id() -> None:
    """幂等契约：WritePoint 请求必须允许调用方携带 command_id。"""
    fields = pb.WritePointRequest.DESCRIPTOR.fields_by_name
    assert "command_id" in fields
    assert fields["command_id"].number == 1


def test_command_result_reports_command_id() -> None:
    fields = pb.CommandResultMessage.DESCRIPTOR.fields_by_name
    assert "command_id" in fields
    assert "success" in fields
    assert "error" in fields


def test_config_transaction_roundtrip_messages_exist() -> None:
    messages = pb.DESCRIPTOR.message_types_by_name
    for stage in ("Prepare", "Activate", "Abort"):
        assert f"{stage}ConfigRequest" in messages
        assert f"{stage}ConfigResponse" in messages


def test_scalar_value_oneof_variants_are_stable() -> None:
    """ScalarValue 是读写值的唯一载体；oneof 分支变化即 wire 契约变化。"""
    oneof = pb.ScalarValue.DESCRIPTOR.oneofs_by_name["kind"]
    assert [f.name for f in oneof.fields] == [
        "bool_value",
        "sint64_value",
        "uint64_value",
        "double_value",
        "string_value",
        "bytes_value",
    ]
