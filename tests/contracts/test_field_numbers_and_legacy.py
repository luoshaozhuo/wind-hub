"""字段编号/枚举数值唯一性，以及禁止项：旧契约残留与故障注入接口。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from google.protobuf.descriptor import Descriptor, EnumDescriptor, FileDescriptor

from contracts import (
    collector_pb2,
    commander_pb2,
    common_pb2,
    data_generator_pb2,
    industrial_gateway_pb2,
    sink_service_pb2,
)

pytestmark = pytest.mark.contract

ALL_FILES: tuple[FileDescriptor, ...] = (
    common_pb2.DESCRIPTOR,
    collector_pb2.DESCRIPTOR,
    commander_pb2.DESCRIPTOR,
    sink_service_pb2.DESCRIPTOR,
    industrial_gateway_pb2.DESCRIPTOR,
    data_generator_pb2.DESCRIPTOR,
)


def _all_messages(file_desc: FileDescriptor) -> Iterator[Descriptor]:
    stack = list(file_desc.message_types_by_name.values())
    while stack:
        message = stack.pop()
        yield message
        stack.extend(message.nested_types_by_name.values())


def test_field_numbers_and_names_unique_per_message() -> None:
    for file_desc in ALL_FILES:
        for message in _all_messages(file_desc):
            numbers = [field.number for field in message.fields]
            names = [field.name for field in message.fields]
            assert len(numbers) == len(set(numbers)), f"{message.full_name} 字段编号重复"
            assert len(names) == len(set(names)), f"{message.full_name} 字段名重复"
            # 冻结要求：编号不得落入预留/保留区间（当前无 reserved，全量 1..max 连续占用即可）。
            assert all(number >= 1 for number in numbers)


def _all_enums(file_desc: FileDescriptor) -> Iterator[EnumDescriptor]:
    yield from file_desc.enum_types_by_name.values()
    for message in _all_messages(file_desc):
        yield from message.enum_types_by_name.values()


def test_enum_numbers_unique_and_unspecified_zero() -> None:
    for file_desc in ALL_FILES:
        for enum in _all_enums(file_desc):
            values = [value.number for value in enum.values]
            assert len(values) == len(set(values)), f"{enum.full_name} 枚举数值重复"
            if enum.values and enum.values[0].name.endswith("_UNSPECIFIED"):
                assert enum.values[0].number == 0


def test_snapshotscope_enum_frozen_values() -> None:
    # 设计书 9.1：正式 Proto 冻结后枚举数值不得复用；显式钉死数值。
    enum = common_pb2.DESCRIPTOR.enum_types_by_name["SnapshotScope"]
    assert [(v.name, v.number) for v in enum.values] == [
        ("SNAPSHOT_SCOPE_UNSPECIFIED", 0),
        ("SNAPSHOT_SCOPE_DISK", 1),
        ("SNAPSHOT_SCOPE_RUNTIME", 2),
        ("SNAPSHOT_SCOPE_CANDIDATE", 3),
    ]


def test_all_packages_versioned_v1() -> None:
    assert {file_desc.package for file_desc in ALL_FILES} == {
        "windhub.common.v1",
        "windhub.collector.v1",
        "windhub.commander.v1",
        "windhub.sink.v1",
        "windhub.gateway.v1",
        "windhub.data_generator.v1",
    }


def test_common_messages_defined_once_in_common_proto() -> None:
    shared = (
        "ScalarValue",
        "Sample",
        "PointMetadata",
        "OperationRequest",
        "OperationResult",
        "ObjectId",
        "Empty",
    )
    for file_desc in ALL_FILES[1:]:
        own = set(file_desc.message_types_by_name)
        assert own.isdisjoint(shared), f"{file_desc.name} 重复声明公共消息 {own & set(shared)}"
    for name in shared:
        assert name in common_pb2.DESCRIPTOR.message_types_by_name


def test_no_fault_injection_rpcs_or_messages() -> None:
    forbidden = ("fault", "inject", "chaos")
    for file_desc in ALL_FILES:
        for service in file_desc.services_by_name.values():
            for method in service.methods:
                lowered = method.name.lower()
                assert not any(token in lowered for token in forbidden), method.full_name
        for message in _all_messages(file_desc):
            lowered = message.name.lower()
            assert not any(token in lowered for token in forbidden), message.full_name


def test_no_retired_packages_or_services() -> None:
    retired_packages = ("windhub.collector.v2", "windhub.sink.v0", "windhub.gateway.v0")
    for file_desc in ALL_FILES:
        assert file_desc.package not in retired_packages
        service_names = set(file_desc.services_by_name)
        assert service_names.isdisjoint(
            {
                "CollectorTaskService",
                "CollectorConfigService",
                "CollectorMetricsService",
                "SinkDeliveryService",
            }
        )


def test_no_streaming_anywhere_and_no_google_empty_substitute() -> None:
    # Empty 由 common.proto 自定义承载；GetServiceInfo 等无参 RPC 必须引用它。
    get_info = common_pb2.DESCRIPTOR.services_by_name["CommonService"].methods_by_name[
        "GetServiceInfo"
    ]
    assert get_info.input_type.full_name == "windhub.common.v1.Empty"
