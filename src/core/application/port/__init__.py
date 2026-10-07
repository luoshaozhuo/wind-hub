"""Shared Core Application ports。"""

from .protocol import (
    ConnectionHealth,
    ProtocolFactoryPort,
    ProtocolPort,
    ProtocolWrite,
    ProtocolWriteResult,
)

__all__ = [
    "ConnectionHealth",
    "ProtocolFactoryPort",
    "ProtocolPort",
    "ProtocolWrite",
    "ProtocolWriteResult",
]
