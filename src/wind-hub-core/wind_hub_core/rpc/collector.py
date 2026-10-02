"""Collector gRPC v1 的共享 wire contract。

本模块只保存稳定的 service/method 名称和 RPC path 构造规则，避免
wind-hub-ctl 与 wind-hub-collector 各自硬编码。它不包含 gRPC server/client
实现，也不引入 Collector Runtime。

当前 v1 消息仍使用 google.protobuf.Empty / Struct；动态消息结构属于过渡契约，
后续可在保持方法名稳定的前提下逐步替换为生成的强类型消息。
"""

RUNTIME_SERVICE = "windhub.collector.v1.CollectorRuntimeService"
CONTROL_SERVICE = "windhub.collector.v1.CollectorControlService"
DIAGNOSTIC_SERVICE = "windhub.collector.v1.CollectorDiagnosticService"

GET_COLLECTOR_INFO = "GetCollectorInfo"
GET_RUNTIME_STATUS = "GetRuntimeStatus"
GET_METRICS_SNAPSHOT = "GetMetricsSnapshot"
LIST_TASKS = "ListTasks"
GET_TASK = "GetTask"
LIST_TASK_INSTANCES = "ListTaskInstances"
GET_TASK_INSTANCE = "GetTaskInstance"
LIST_DEVICES = "ListDevices"
LIST_SINKS = "ListSinks"
VERIFY_SINK = "VerifySink"
WRITE_TEST_SINK = "WriteTestSink"
READ_POINT = "ReadPoint"

START_TASK = "StartTask"
STOP_TASK = "StopTask"
START_TASK_INSTANCE = "StartTaskInstance"
STOP_TASK_INSTANCE = "StopTaskInstance"
START_ASSIGNED_TASKS = "StartAssignedTasks"
STOP_ASSIGNED_TASKS = "StopAssignedTasks"
WRITE_POINT = "WritePoint"
RELOAD_CONFIG = "ReloadConfig"

VERIFY_DEVICE = "VerifyDevice"
RESOLVE_POINT = "ResolvePoint"
VERIFY_POINT = "VerifyPoint"
VERIFY_POINTS = "VerifyPoints"


def rpc_path(service: str, method: str) -> str:
    """构造 gRPC fully-qualified method path。

    Args:
        service: 完整 service 名称。
        method: method 名称。

    Returns:
        形如 /package.Service/Method 的 gRPC 方法路径。
    """
    return f"/{service}/{method}"
