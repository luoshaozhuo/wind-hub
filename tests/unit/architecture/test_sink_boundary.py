"""Collector Sink application boundary 约束（Phase 6）。

Collector gRPC adapter 必须经 ``CollectorSinkService`` 访问运行 Sink，
不得穿透 application 层直接操作 Sink 注册表或 SinkPort 实例。

扫描基于少量稳定 token，不绑定精确实现行。
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GRPC_ADAPTER = (
    REPO_ROOT
    / "src"
    / "wind_hub_collector"
    / "adapter"
    / "inbound"
    / "grpc"
    / "server.py"
)

# 出现即代表 adapter 直接穿透到 Sink 注册表 / SinkPort 实例。
_FORBIDDEN_TOKENS = (
    "SinkPort",
    "runtime.sinks",
    "sink.write",
    "sink.flush",
)


def test_grpc_adapter_does_not_touch_sink_instances() -> None:
    """gRPC adapter 不直接访问 Sink 注册表与 SinkPort。"""
    source = GRPC_ADAPTER.read_text(encoding="utf-8")
    offenders = [token for token in _FORBIDDEN_TOKENS if token in source]
    assert offenders == []
