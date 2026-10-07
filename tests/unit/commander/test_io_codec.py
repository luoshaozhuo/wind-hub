"""新 Commander gRPC I/O codec 单元测试。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from commander.application.command import Command, CommandResult
from commander.application.session import PointReading
from commander.infrastructure.grpc import commander_pb2 as pb
from commander.infrastructure.grpc.io_codec import (
    command_from_proto,
    command_result_to_proto,
    command_to_proto,
    decode_scalar,
    encode_scalar,
    point_value_to_proto,
)
from core.application import Quality


@pytest.mark.parametrize(
    "value",
    [True, 0, -1, 2**63 - 1, 2**63, 2**64 - 1, 1.5, "text", b"\x01\x02"],
)
def test_scalar_roundtrip(value):
    assert decode_scalar(encode_scalar(value)) == value


def test_scalar_none_is_unset():
    message = encode_scalar(None)
    assert message.WhichOneof("kind") is None
    assert decode_scalar(message) is None


def test_scalar_int_out_of_range_rejected():
    with pytest.raises(ValueError, match="64-bit"):
        encode_scalar(2**64)
    with pytest.raises(ValueError, match="64-bit"):
        encode_scalar(-(2**63) - 1)


def test_scalar_unsupported_type_rejected():
    with pytest.raises(ValueError, match="unsupported"):
        encode_scalar(object())


def test_point_value_to_proto_with_and_without_timestamp():
    reading = PointReading(
        device_id="dev1",
        point_id="p1",
        value=1.5,
        quality=Quality.GOOD,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        source="modbus",
    )
    message = point_value_to_proto(reading)
    assert message.device_id == "dev1"
    assert message.value.double_value == 1.5
    assert message.quality == "good"
    assert message.HasField("timestamp")

    no_ts = PointReading(
        device_id="dev1",
        point_id="p1",
        value=None,
        quality=Quality.BAD,
        timestamp=None,
        source="modbus",
    )
    message = point_value_to_proto(no_ts)
    assert message.value.WhichOneof("kind") is None
    assert message.quality == "bad"


def test_command_roundtrip_and_result():
    command = Command(
        command_id="cmd-1",
        device_id="dev1",
        point_id="p1",
        value=42,
        timeout=2.5,
    )
    wire = command_to_proto(command)
    restored = command_from_proto(wire)
    # issued_at 由 Command 默认工厂生成，不进入 wire contract
    assert restored.command_id == command.command_id
    assert restored.device_id == command.device_id
    assert restored.point_id == command.point_id
    assert restored.value == command.value
    assert restored.timeout == command.timeout

    result = CommandResult(command_id="cmd-1", success=True)
    message = command_result_to_proto(result)
    assert message.command_id == "cmd-1"
    assert message.success is True
    assert message.error == ""
    assert message.HasField("finished_at")

    failed = CommandResult(command_id="cmd-2", success=False, error="boom")
    message = command_result_to_proto(failed)
    assert message.error == "boom"


def test_pb_module_is_per_process_copy():
    """pb2 模块必须来自 commander 进程内副本（不依赖 wind_hub_core）。"""
    assert pb.__name__.startswith("commander.infrastructure.grpc")
    assert "wind_hub" not in str(pb.__file__)
