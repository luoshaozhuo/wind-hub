"""Collector gRPC v2 wire contract 契约测试。
该契约是 wind-hub CLI / Web Admin 与 Collector 之间唯一的耦合点：service /
method 名称与 path 构造规则一旦漂移，两侧会在运行时以 UNIMPLEMENTED
失败。本测试直接从生成的 pb2 描述符（真实 wire 定义）读取并固化为显式
断言——任何 proto 改名都必须同步修改本文件，从而强制契约变更显式化。
"""

from __future__ import annotations

import re

import pytest

from contracts import collector_v2_pb2 as pb

pytestmark = pytest.mark.contract

_PACKAGE = "windhub.collector.v2"

_RUNTIME_METHODS = [
    "GetCollectorInfo",
    "GetRuntimeStatus",
    "ListDevices",
    "GetDevice",
    "GetLatestValues",
    "GetPointMetadata",
]

_TASK_METHODS = [
    "ListTasks",
    "GetTask",
    "StartTask",
    "StopTask",
]

_CONFIG_METHODS = [
    "GetConfig",
    "ValidateConfig",
    "UpdateConfig",
    "ReplaceConfig",
    "GetConfigStatus",
]

_METRICS_METHODS = [
    "GetMetrics",
    "GetMetricsEvents",
]

_METHODS_BY_SERVICE = {
    "CollectorRuntimeService": _RUNTIME_METHODS,
    "CollectorTaskService": _TASK_METHODS,
    "CollectorConfigService": _CONFIG_METHODS,
    "CollectorMetricsService": _METRICS_METHODS,
}


def _service(name: str):
    try:
        return pb.DESCRIPTOR.services_by_name[name]
    except KeyError:
        raise AssertionError(f"service '{name}' missing from collector_v2.proto") from None


def test_package_name_is_stable() -> None:
    assert pb.DESCRIPTOR.package == _PACKAGE


def test_service_set_is_stable() -> None:
    assert set(pb.DESCRIPTOR.services_by_name) == set(_METHODS_BY_SERVICE)


@pytest.mark.parametrize(("service_name", "methods"), list(_METHODS_BY_SERVICE.items()))
def test_service_methods_are_stable(service_name: str, methods: list[str]) -> None:
    service = _service(service_name)
    assert [m.name for m in service.methods] == methods


def test_method_names_follow_grpc_pascal_case() -> None:
    methods = [
        method.name
        for service in pb.DESCRIPTOR.services_by_name.values()
        for method in service.methods
    ]
    assert methods, "expected methods in collector_v2.proto"
    for method in methods:
        assert re.fullmatch(r"[A-Z][A-Za-z0-9]*", method), method


def test_method_names_are_unique_across_services() -> None:
    methods = [
        method.name
        for service in pb.DESCRIPTOR.services_by_name.values()
        for method in service.methods
    ]
    # 控制面方法与查询面方法不得撞名——path 按 /package.Service/Method
    # 构造，跨 service 重名会放大改名时的排障难度。
    assert len(methods) == len(set(methods))


def test_fully_qualified_method_paths() -> None:
    for service_name, service in pb.DESCRIPTOR.services_by_name.items():
        for method in service.methods:
            path = f"/{_PACKAGE}.{service_name}/{method.name}"
            # gRPC path 形如 /package.Service/Method，前后不得有多余斜杠。
            assert path.startswith("/") and not path.endswith("/")
            assert path.count("/") == 2


def test_config_rpc_messages_exist() -> None:
    """五个 Config RPC 的 wire message 必须成对存在。"""
    messages = pb.DESCRIPTOR.message_types_by_name
    pairs = [
        ("GetConfig", "GetConfigRequest", "GetConfigResponse"),
        ("ValidateConfig", "ValidateConfigRequest", "ValidateConfigResponse"),
        ("UpdateConfig", "UpdateConfigRequest", "ConfigMutationResponse"),
        ("ReplaceConfig", "ReplaceConfigRequest", "ConfigMutationResponse"),
        ("GetConfigStatus", "GetConfigStatusRequest", "GetConfigStatusResponse"),
    ]
    for _rpc, request_name, response_name in pairs:
        assert request_name in messages
        assert response_name in messages
