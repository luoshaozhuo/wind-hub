"""Collector gRPC v1 wire contract 契约测试。

该契约是 Server / wind-hub-ctl 与 Collector 之间唯一的耦合点：service /
method 名称与 path 构造规则一旦漂移，两侧会在运行时以 UNIMPLEMENTED
失败。本测试直接从生成的 pb2 描述符（真实 wire 定义）读取并固化为显式
断言——任何 proto 改名都必须同步修改本文件，从而强制契约变更显式化。
"""

from __future__ import annotations

import re

from wind_hub_core.rpc import collector_pb2 as pb

_PACKAGE = "windhub.collector.v1"

_RUNTIME_METHODS = [
    "GetCollectorInfo",
    "GetRuntimeStatus",
    "GetMetricsSnapshot",
    "ListTasks",
    "GetTask",
    "ListTaskInstances",
    "GetTaskInstance",
    "ListDevices",
    "ListSinks",
    "VerifySink",
    "WriteTestSink",
    "GetLatestTelemetry",
    "GetTelemetryTrend",
]

_CONTROL_METHODS = [
    "ApplyTaskPlacement",
    "StartTask",
    "StopTask",
    "StartTaskInstance",
    "StopTaskInstance",
    "PrepareConfig",
    "ActivateConfig",
    "AbortConfig",
]


def _service(name: str):
    try:
        return pb.DESCRIPTOR.services_by_name[name]
    except KeyError:
        raise AssertionError(f"service '{name}' missing from collector.proto") from None


def test_package_name_is_stable() -> None:
    assert pb.DESCRIPTOR.package == _PACKAGE


def test_service_set_is_stable() -> None:
    assert set(pb.DESCRIPTOR.services_by_name) == {
        "CollectorRuntimeService",
        "CollectorControlService",
    }


def test_runtime_service_methods_are_stable() -> None:
    service = _service("CollectorRuntimeService")
    assert [m.name for m in service.methods] == _RUNTIME_METHODS


def test_control_service_methods_are_stable() -> None:
    service = _service("CollectorControlService")
    assert [m.name for m in service.methods] == _CONTROL_METHODS


def test_method_names_follow_grpc_pascal_case() -> None:
    methods = [
        method.name
        for service in pb.DESCRIPTOR.services_by_name.values()
        for method in service.methods
    ]
    assert methods, "expected methods in collector.proto"
    for method in methods:
        assert re.fullmatch(r"[A-Z][A-Za-z0-9]*", method), method


def test_method_names_are_unique_across_services() -> None:
    methods = [
        method.name
        for service in pb.DESCRIPTOR.services_by_name.values()
        for method in service.methods
    ]
    # ApplyTaskPlacement 等控制面方法与查询面方法不得撞名——path 按
    # /package.Service/Method 构造，跨 service 重名会放大改名时的排障难度。
    assert len(methods) == len(set(methods))


def test_fully_qualified_method_paths() -> None:
    for service_name, service in pb.DESCRIPTOR.services_by_name.items():
        for method in service.methods:
            path = f"/{_PACKAGE}.{service_name}/{method.name}"
            # gRPC path 形如 /package.Service/Method，前后不得有多余斜杠。
            assert path.startswith("/") and not path.endswith("/")
            assert path.count("/") == 2


def test_config_transaction_roundtrip_messages_exist() -> None:
    """Prepare / Activate / Abort 三段式配置事务的 wire message 必须成对存在。"""
    messages = pb.DESCRIPTOR.message_types_by_name
    for stage in ("Prepare", "Activate", "Abort"):
        assert f"{stage}ConfigRequest" in messages
        assert f"{stage}ConfigResponse" in messages
