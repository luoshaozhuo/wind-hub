"""CTL 编解码与参数解析单元测试。"""

from __future__ import annotations

import pytest

from core.infrastructure.rpc import collector_pb2
from ctl.client import _to_dict, encode_scalar
from ctl.main import build_parser


def test_to_dict_preserves_uint64_as_int() -> None:
    """protobuf uint64 输出保持 JSON number，而不是字符串。"""
    payload = _to_dict(
        collector_pb2.RuntimeStatusResponse(
            running=True,
            device_count=3,
            points_collected=2**53 + 1,
        )
    )

    assert payload["device_count"] == 3
    assert payload["points_collected"] == 2**53 + 1


def test_encode_scalar_keeps_bool_before_int() -> None:
    """bool 不得因 bool 是 int 子类而被编码为 sint64。"""
    value = encode_scalar(True)

    assert value.WhichOneof("kind") == "bool_value"
    assert value.bool_value is True


def test_encode_scalar_supports_unsigned_64_bit_range() -> None:
    """超过 int64 正数范围后切换到 uint64。"""
    value = encode_scalar(2**63)

    assert value.WhichOneof("kind") == "uint64_value"
    assert value.uint64_value == 2**63


def test_parser_uses_separate_collector_and_commander_targets() -> None:
    """两进程控制面拥有独立默认 endpoint。"""
    parser = build_parser()

    collector = parser.parse_args(["collector", "status"])
    commander = parser.parse_args(["commander", "status"])

    assert collector.target == "127.0.0.1:50051"
    assert commander.target == "127.0.0.1:50052"


def test_parser_decodes_write_scalar() -> None:
    """write 标量优先按 JSON scalar 解析。"""
    args = build_parser().parse_args(
        ["commander", "write", "dev1", "p1", "12.5"]
    )

    assert args.value == 12.5


@pytest.mark.asyncio
async def test_cli_write_failure_returns_nonzero(monkeypatch, capsys) -> None:
    """RPC 返回失败结果时 CLI 不得报告成功。"""
    from ctl import main as cli

    class FailedClient:
        def __init__(self, target: str, *, timeout: float) -> None:
            del target, timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def call(self, operation: str, **kwargs):
            del operation, kwargs
            return {"command_id": "failed", "success": False, "error": "offline"}

    monkeypatch.setattr(cli, "CommanderClient", FailedClient)
    args = cli.build_parser().parse_args(
        ["commander", "write", "dev1", "p1", "12"]
    )

    assert await cli._run(args) == 2
    assert '"success": false' in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cli_partial_batch_write_failure_returns_nonzero(monkeypatch) -> None:
    """任一批量写失败均以失败退出码返回。"""
    from ctl import main as cli

    class PartialClient:
        def __init__(self, target: str, *, timeout: float) -> None:
            del target, timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def call(self, operation: str, **kwargs):
            del operation, kwargs
            return {"results": [{"success": True}, {"success": False}]}

    monkeypatch.setattr(cli, "CommanderClient", PartialClient)
    args = cli.build_parser().parse_args(
        ["commander", "write-many", "--item", "dev1", "p1", "1"]
    )

    assert await cli._run(args) == 2
