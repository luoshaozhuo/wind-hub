"""Commander 集成测试夹具：复用跨层共享 gRPC harness（tests/commander/grpc_harness.py）。

真实 mTLS PKI、build_commander 组合根与仅安全端口服务器的构造在共享 harness
中实现，供 integration 与 system 两层复用，避免链路构造分叉。

NATS 在本层多数用例中不可达（127.0.0.1:1），审计依赖降级不影响 query/
snapshot/accept 路径；需要真实 JetStream 的断言放在 system 层并显式 skip。
"""

from __future__ import annotations

import pytest

from tests.commander.grpc_harness import (
    IDENTITIES,
    STRANGER_IDENTITY,
    GrpcStack,
    Pki,
    build_pki,
    build_stack,
)

__all__ = [
    "IDENTITIES",
    "STRANGER_IDENTITY",
    "GrpcStack",
    "Pki",
    "pki",
    "grpc_stack",
    "grpc_stack_modbus",
]


@pytest.fixture
def pki(tmp_path) -> Pki:
    return build_pki(tmp_path)


@pytest.fixture
async def grpc_stack(tmp_path, pki: Pki) -> GrpcStack:
    stack, server, service = await build_stack(
        tmp_path, pki, label="integration", stream_prefix="CMD_IT"
    )
    try:
        yield stack
    finally:
        await server.stop(grace=5)
        await service.stop()


@pytest.fixture
async def grpc_stack_modbus(tmp_path, pki: Pki, modbus_server) -> GrpcStack:
    """额外注入 WT01，指向真实 Modbus TCP 对端（pymodbus 模拟从站）。"""
    stack, server, service = await build_stack(
        tmp_path,
        pki,
        modbus_port=modbus_server.port,
        label="integration",
        stream_prefix="CMD_IT",
    )
    try:
        yield stack
    finally:
        await server.stop(grace=5)
        await service.stop()
