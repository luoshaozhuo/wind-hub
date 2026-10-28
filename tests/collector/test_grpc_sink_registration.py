"""GrpcSink 是注册的通用输出类型，不改变 ModbusSink Server 语义。"""

from __future__ import annotations

import pytest

from collector.infrastructure.sink_registry import build_sink_registry
from core.infrastructure.config.sink_schema import parse_sink_definitions

pytestmark = pytest.mark.unit


def test_grpc_sink_schema_and_registration() -> None:
    parsed = parse_sink_definitions(
        {
            "sinks": {
                "gateway": {
                    "type": "grpc",
                    "connection": {"endpoint": "127.0.0.1:50073"},
                }
            }
        }
    )
    assert parsed["gateway"].type == "grpc"
    assert "grpc" in build_sink_registry().registered_types()
