# -*- coding: utf-8 -*-
"""Generated-compatible protobuf messages for commander_io.proto."""

from google.protobuf import descriptor_pool as _descriptor_pool
from google.protobuf import symbol_database as _symbol_database
from google.protobuf.internal import builder as _builder
from google.protobuf import timestamp_pb2 as google_dot_protobuf_dot_timestamp__pb2

_sym_db = _symbol_database.Default()

DESCRIPTOR = _descriptor_pool.Default().AddSerializedFile(b'\n*wind_hub_core/rpc/proto/commander_io.proto\x12\x14windhub.commander.v1\x1a\x1fgoogle/protobuf/timestamp.proto"\'\n\x0bScalarValue\x12\x14\n\nbool_value\x18\x01 \x01(\x08H\x00\x12\x16\n\x0csint64_value\x18\x02 \x01(\x12H\x00\x12\x16\n\x0cuint64_value\x18\x03 \x01(\x04H\x00\x12\x16\n\x0cdouble_value\x18\x04 \x01(\x01H\x00\x12\x16\n\x0cstring_value\x18\x05 \x01(\tH\x00\x12\x15\n\x0bbytes_value\x18\x06 \x01(\x0cH\x00B\x06\n\x04kind"7\n\x10ReadPointRequest\x12\x11\n\tdevice_id\x18\x01 \x01(\t\x12\x10\n\x08point_id\x18\x02 \x01(\t"8\n\x11ReadPointsRequest\x12\x11\n\tdevice_id\x18\x01 \x01(\t\x12\x10\n\tpoint_ids\x18\x02 \x03(\t"\xc8\x01\n\x11PointValueMessage\x12\x11\n\tdevice_id\x18\x01 \x01(\t\x12\x10\n\x08point_id\x18\x02 \x01(\t\x12/\n\x05value\x18\x03 \x01(\x0b2 .windhub.commander.v1.ScalarValue\x12\x0f\n\x07quality\x18\x04 \x01(\t\x12.\n\ttimestamp\x18\x05 \x01(\x0b2\x1a.google.protobuf.Timestamp\x12\x0e\n\x06source\x18\x06 \x01(\t"G\n\x12ReadPointsResponse\x121\n\x06values\x18\x01 \x03(\x0b2!.windhub.commander.v1.PointValueMessage"\x9c\x01\n\x11WritePointRequest\x12\x12\n\ncommand_id\x18\x01 \x01(\t\x12\x11\n\tdevice_id\x18\x02 \x01(\t\x12\x10\n\x08point_id\x18\x03 \x01(\t\x12/\n\x05value\x18\x04 \x01(\x0b2 .windhub.commander.v1.ScalarValue\x12\x0f\n\x07timeout\x18\x05 \x01(\x01"J\n\x12WritePointsRequest\x124\n\x08commands\x18\x01 \x03(\x0b2".windhub.commander.v1.WritePointRequest"\x8e\x01\n\x14CommandResultMessage\x12\x12\n\ncommand_id\x18\x01 \x01(\t\x12\x0f\n\x07success\x18\x02 \x01(\x08\x12\r\n\x05error\x18\x03 \x01(\t\x12.\n\x0bfinished_at\x18\x04 \x01(\x0b2\x1a.google.protobuf.Timestamp"L\n\x13WritePointsResponse\x125\n\x07results\x18\x01 \x03(\x0b2$.windhub.commander.v1.CommandResultMessageb\x06proto3')

_builder.BuildMessageAndEnumDescriptors(DESCRIPTOR, globals())
_builder.BuildTopDescriptorsAndMessages(
    DESCRIPTOR,
    "wind_hub_core.rpc.commander_io_pb2",
    globals(),
)
