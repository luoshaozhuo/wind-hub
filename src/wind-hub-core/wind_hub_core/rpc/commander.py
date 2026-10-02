"""Commander gRPC v1 共享 wire contract。

当前仍使用 google.protobuf.Struct 作为过渡消息，仅在这里集中定义稳定的
service/method 名称，避免 Server/CLI/Commander 各自硬编码 RPC path。
"""

COMMANDER_SERVICE = "windhub.commander.v1.CommanderService"

GET_STATUS = "GetStatus"
LIST_DEVICES = "ListDevices"
READ_POINT = "ReadPoint"
READ_POINTS = "ReadPoints"
WRITE_POINT = "WritePoint"
WRITE_POINTS = "WritePoints"
VERIFY_DEVICE = "VerifyDevice"
RESOLVE_POINT = "ResolvePoint"
VERIFY_POINT = "VerifyPoint"
VERIFY_POINTS = "VerifyPoints"


def rpc_path(method: str) -> str:
    """构造 Commander gRPC fully-qualified method path。"""
    return f"/{COMMANDER_SERVICE}/{method}"
