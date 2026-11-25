"""Normalize protocol point declarations to shared Core scalar types.

A physical Storage schema uses this authoritative normalization rather than
inventing a second protocol-specific type vocabulary.
"""

from __future__ import annotations

from core.domain.value_objects import DataType

_ADS_CANONICAL = {
    "BOOL": DataType.BOOL,
    "SINT": DataType.INT8,
    "USINT": DataType.UINT8,
    "INT": DataType.INT16,
    "UINT": DataType.UINT16,
    "DINT": DataType.INT32,
    "UDINT": DataType.UINT32,
    "LINT": DataType.INT64,
    "ULINT": DataType.UINT64,
    "REAL": DataType.FLOAT32,
    "LREAL": DataType.FLOAT64,
    "STRING": DataType.STRING,
}


def normalize_protocol_scalar_type(protocol: str, declared_type: str) -> DataType:
    """Return a lossless canonical type or reject unsupported declarations."""
    if not isinstance(protocol, str) or not isinstance(declared_type, str):
        raise ValueError("protocol and scalar type must be strings")
    normalized_protocol = protocol.lower().strip()
    if normalized_protocol not in {"ads", "modbus", "iec104"}:
        raise ValueError(f"unsupported point-table protocol: {protocol}")
    token = declared_type.strip()
    if not token:
        raise ValueError("empty point data_type")
    if normalized_protocol == "ads":
        result = _ADS_CANONICAL.get(token.upper())
        if result is not None:
            return result
    try:
        return DataType(token.lower())
    except ValueError as exc:
        raise ValueError(f"unsupported {normalized_protocol} data_type: {token}") from exc
