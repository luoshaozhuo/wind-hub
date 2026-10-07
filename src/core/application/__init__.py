"""Shared Core Application 用例与稳定数据契约。"""

from .collection_plan import (
    CollectionAssignment,
    CollectionWork,
    assign_collection_connection,
    build_collection_work,
)
from .interpretation import interpret_protocol_sample
from .measurement import PointScalar, PointValue, ProtocolSample, Quality
from .port import (
    AcquisitionMode,
    ExclusiveOpenSinkPort,
    ProtocolPort,
    ProtocolWrite,
    ProtocolWriteResult,
    SinkPort,
    SubscribableProtocolPort,
    SubscriptionHandle,
)

__all__ = [
    "AcquisitionMode",
    "CollectionAssignment",
    "CollectionWork",
    "ExclusiveOpenSinkPort",
    "PointScalar",
    "PointValue",
    "ProtocolPort",
    "ProtocolSample",
    "ProtocolWrite",
    "ProtocolWriteResult",
    "Quality",
    "SinkPort",
    "SubscribableProtocolPort",
    "SubscriptionHandle",
    "assign_collection_connection",
    "build_collection_work",
    "interpret_protocol_sample",
]
