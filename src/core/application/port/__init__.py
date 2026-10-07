"""Shared Core Application ports。"""

from .protocol import (
    ProtocolFactoryPort,
    ProtocolPort,
    ProtocolWrite,
    ProtocolWriteResult,
)

__all__ = [
    "ProtocolFactoryPort",
    "ProtocolPort",
    "ProtocolWrite",
    "ProtocolWriteResult",
]
