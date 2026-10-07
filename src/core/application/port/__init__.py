"""Shared Core Application ports。"""

from .protocol import (
    ConnectionHealth,
    InterrogationCapableProtocolPort,
    ProtocolFactoryPort,
    ProtocolPort,
    ProtocolWrite,
    ProtocolWriteResult,
    SubscribableProtocolPort,
    SubscriptionHandle,
)

__all__ = [
    "ConnectionHealth",
    "InterrogationCapableProtocolPort",
    "ProtocolFactoryPort",
    "ProtocolPort",
    "ProtocolWrite",
    "ProtocolWriteResult",
    "SubscribableProtocolPort",
    "SubscriptionHandle",
]
