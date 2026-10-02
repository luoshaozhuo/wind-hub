"""Collector gRPC v1 wire contract 单元测试。

该契约是 wind-hub-ctl 与 wind-hub-collector 之间唯一的耦合点：service /
method 名称与 path 构造规则一旦漂移，两侧会在运行时以 UNIMPLEMENTED
失败。本测试把名称表固化为显式断言——任何改名都必须同步修改本文件，
从而强制契约变更显式化。
"""

from __future__ import annotations

import re

from wind_hub_core.rpc import collector as rpc

_SERVICE_PREFIX = "windhub.collector.v1."


def test_service_names_are_stable() -> None:
    assert rpc.RUNTIME_SERVICE == "windhub.collector.v1.CollectorRuntimeService"
    assert rpc.CONTROL_SERVICE == "windhub.collector.v1.CollectorControlService"
    assert rpc.DIAGNOSTIC_SERVICE == "windhub.collector.v1.CollectorDiagnosticService"


def test_runtime_service_methods_are_stable() -> None:
    assert rpc.GET_COLLECTOR_INFO == "GetCollectorInfo"
    assert rpc.GET_RUNTIME_STATUS == "GetRuntimeStatus"
    assert rpc.LIST_TASKS == "ListTasks"
    assert rpc.GET_TASK == "GetTask"
    assert rpc.LIST_TASK_INSTANCES == "ListTaskInstances"
    assert rpc.GET_TASK_INSTANCE == "GetTaskInstance"
    assert rpc.LIST_DEVICES == "ListDevices"
    assert rpc.READ_POINT == "ReadPoint"


def test_control_service_methods_are_stable() -> None:
    assert rpc.START_TASK == "StartTask"
    assert rpc.STOP_TASK == "StopTask"
    assert rpc.START_TASK_INSTANCE == "StartTaskInstance"
    assert rpc.STOP_TASK_INSTANCE == "StopTaskInstance"
    assert rpc.START_ASSIGNED_TASKS == "StartAssignedTasks"
    assert rpc.STOP_ASSIGNED_TASKS == "StopAssignedTasks"
    assert rpc.WRITE_POINT == "WritePoint"
    assert rpc.RELOAD_CONFIG == "ReloadConfig"


def test_diagnostic_service_methods_are_stable() -> None:
    assert rpc.VERIFY_DEVICE == "VerifyDevice"
    assert rpc.RESOLVE_POINT == "ResolvePoint"
    assert rpc.VERIFY_POINT == "VerifyPoint"
    assert rpc.VERIFY_POINTS == "VerifyPoints"


def test_method_names_follow_grpc_pascal_case() -> None:
    methods = [
        value
        for name, value in vars(rpc).items()
        if name.isupper() and isinstance(value, str) and not value.startswith(_SERVICE_PREFIX)
    ]
    assert methods, "expected method constants in rpc.collector"
    for method in methods:
        assert re.fullmatch(r"[A-Z][A-Za-z0-9]*", method), method


def test_method_names_are_unique_across_services() -> None:
    methods = [
        value
        for name, value in vars(rpc).items()
        if name.isupper() and isinstance(value, str) and not value.startswith(_SERVICE_PREFIX)
    ]
    assert len(methods) == len(set(methods))


def test_rpc_path_builds_fully_qualified_method_path() -> None:
    path = rpc.rpc_path(rpc.CONTROL_SERVICE, rpc.START_TASK)
    assert path == "/windhub.collector.v1.CollectorControlService/StartTask"
    # gRPC path 形如 /package.Service/Method，前后不得有多余斜杠。
    assert path.startswith("/") and not path.endswith("/")
    assert path.count("/") == 2
