"""Reusable Redis and Modbus output adapters."""

from .modbus import ModbusSink
from .redis import RedisSink

__all__ = ["ModbusSink", "RedisSink"]
