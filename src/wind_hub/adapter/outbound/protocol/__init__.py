"""Protocol adapters — import all drivers to trigger self-registration."""

from wind_hub.adapter.outbound.protocol.ads.driver import ADSDriver  # noqa: F401
from wind_hub.adapter.outbound.protocol.iec104.driver import IEC104Driver  # noqa: F401
from wind_hub.adapter.outbound.protocol.modbus.driver import ModbusDriver  # noqa: F401

__all__ = ["ADSDriver", "IEC104Driver", "ModbusDriver"]
