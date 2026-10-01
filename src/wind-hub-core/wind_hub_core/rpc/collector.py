"""Collector gRPC v1 稳定方法名。

这里只保存 wire-level service/method 名称，避免 wind-hub-ctl 与 Collector
各自硬编码。消息目前仍使用 google.protobuf.Empty / Struct。
"""

RUNTIME_SERVICE = "windhub.collector.v1.CollectorRuntimeService"
CONTROL_SERVICE = "windhub.collector.v1.CollectorControlService"

GET_COLLECTOR_INFO = "GetCollectorInfo"
GET_RUNTIME_STATUS = "GetRuntimeStatus"
LIST_TASK_INSTANCES = "ListTaskInstances"
GET_TASK_INSTANCE = "GetTaskInstance"
LIST_DEVICES = "ListDevices"
READ_POINT = "ReadPoint"

START_TASK_INSTANCE = "StartTaskInstance"
STOP_TASK_INSTANCE = "StopTaskInstance"
START_ASSIGNED_TASKS = "StartAssignedTasks"
STOP_ASSIGNED_TASKS = "StopAssignedTasks"
WRITE_POINT = "WritePoint"


def rpc_path(service: str, method: str) -> str:
    """构造 gRPC fully-qualified method path。"""
    return f"/{service}/{method}"
