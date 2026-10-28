"""进程间 RPC 的传输中立辅助件。

强类型 protobuf 契约由独立的 ``contracts`` 包提供；本包仅保留与具体
Stub 无关的关联 ID 管理（``correlation``）与标量编解码（``scalar_codec``）。
"""
