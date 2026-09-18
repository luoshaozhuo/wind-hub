"""IEC104 slave proxy (从站模式) inbound adapter.

Lets wind-hub act as an IEC 60870-5-104 **server** (slave): it listens for
a dispatch master, answers general interrogation from the live data snapshot,
and accepts remote-control commands routed through the engine Dispatcher.

Reuses the pure :mod:`~wind_hub.adapter.outbound.protocol.iec104.codec`
encode/decode layer; depends on the :class:`~wind_hub.domain.engine.scheduler`
observer mechanism and the :class:`~wind_hub.domain.engine.dispatcher`.
"""

from wind_hub.adapter.inbound.iec104_slave.bridge import SchedulerBridge
from wind_hub.adapter.inbound.iec104_slave.buffer import DataSnapshot
from wind_hub.adapter.inbound.iec104_slave.handlers import IEC104SlaveHandlers
from wind_hub.adapter.inbound.iec104_slave.mapping import (
    build_data_type_mapping,
    build_ioa_mapping,
    build_reverse_mapping,
)
from wind_hub.adapter.inbound.iec104_slave.server import IEC104SlaveServer
from wind_hub.adapter.inbound.iec104_slave.session import IEC104SlaveSession

__all__ = [
    "DataSnapshot",
    "build_ioa_mapping",
    "build_data_type_mapping",
    "build_reverse_mapping",
    "SchedulerBridge",
    "IEC104SlaveHandlers",
    "IEC104SlaveSession",
    "IEC104SlaveServer",
]
