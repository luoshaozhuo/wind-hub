"""Modbus point mapping — parse point addresses and group reads.

A Modbus point address is a :class:`~wind_hub.config.schema.PointAddress`
whose ``extra`` fields carry::

    register_type: "coil" | "discrete_input" | "holding" | "input"
    address:      0-based register/coil offset
    count:        (optional) number of coils/registers — derived from
                  ``data_type`` when omitted
    byte_order:   (optional) "big_endian" (default) | "little_endian"
"""

from __future__ import annotations

from dataclasses import dataclass

from wind_hub.config.schema import PointAddress, PointConfig
from wind_hub.domain.model.errors import ConfigError

# Canonical register-type names plus accepted aliases.
_REGISTER_TYPE_ALIASES: dict[str, str] = {
    "coil": "coil",
    "discrete_input": "discrete_input",
    "discrete": "discrete_input",
    "input": "input",
    "input_register": "input",
    "holding": "holding",
    "holding_register": "holding",
}

_VALID_BYTE_ORDERS = frozenset({"big_endian", "little_endian"})

# Number of 16-bit registers occupied by each data type (for holding/input).
_REGISTER_COUNTS: dict[str, int] = {
    "bool": 1,
    "int8": 1,
    "uint8": 1,
    "int16": 1,
    "uint16": 1,
    "int32": 2,
    "uint32": 2,
    "float32": 2,
    "int64": 4,
    "uint64": 4,
    "float64": 4,
}


@dataclass(frozen=True)
class ModbusPoint:
    """A single Modbus point with its resolved address and type."""

    point_id: str
    register_type: str
    """One of ``"coil"``, ``"discrete_input"``, ``"holding"``, ``"input"``."""

    address: int
    """0-based coil/register offset."""

    count: int
    """Number of coils (bit types) or 16-bit registers (word types)."""

    data_type: str
    """Declared point data type (e.g. ``"float32"``)."""

    byte_order: str = "big_endian"
    """Multi-register byte order: ``"big_endian"`` or ``"little_endian"``."""


def _extra(address: PointAddress, key: str) -> object:
    """Read an extra field from a :class:`PointAddress` (``None`` if absent)."""
    return (address.model_extra or {}).get(key)


def _as_int(value: object, what: str) -> int:
    """Coerce an extra field to ``int``, rejecting non-integer values.

    ``bool`` is rejected explicitly: a coil/register offset is never
    truth-valued, and ``bool`` is a subclass of ``int`` that would otherwise
    slip through ``isinstance`` checks.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{what}: must be an integer, got {type(value).__name__}")
    return value


def _normalize_register_type(raw: object) -> str:
    if raw is None:
        raise ConfigError(
            "Modbus point: missing 'register_type' in address "
            "(expected coil/discrete_input/holding/input)"
        )
    key = str(raw).lower()
    if key not in _REGISTER_TYPE_ALIASES:
        raise ConfigError(
            f"Modbus point: invalid register_type '{raw}'; "
            f"must be one of coil/discrete_input/holding/input"
        )
    return _REGISTER_TYPE_ALIASES[key]


def _count_for_data_type(register_type: str, data_type: str) -> int:
    if register_type in ("coil", "discrete_input"):
        # Bit-addressable: one point occupies one coil regardless of data type.
        return 1
    count = _REGISTER_COUNTS.get(data_type)
    if count is None:
        raise ConfigError(
            f"Modbus point: data_type '{data_type}' is not supported for "
            f"register reads (supported: {sorted(_REGISTER_COUNTS)})"
        )
    return count


def parse_point(point: PointConfig, default_byte_order: str = "big_endian") -> ModbusPoint:
    """Resolve a :class:`~wind_hub.config.schema.PointConfig` to a :class:`ModbusPoint`.

    The ``register_type`` may be given in the ``address`` extra field, or via
    the ``type`` field of the address model.  ``count`` defaults to the
    register/coil size implied by ``data_type``.

    Raises:
        ConfigError: On a missing/invalid ``register_type``, missing/negative
            ``address``, non-positive ``count``, or an invalid ``byte_order``.
    """
    address = point.address
    register_type = _normalize_register_type(
        _extra(address, "register_type")
        if _extra(address, "register_type") is not None
        else address.type
    )

    raw_addr = _extra(address, "address")
    if raw_addr is None:
        raise ConfigError(f"Modbus point '{point.point_id}': missing 'address'")
    addr = _as_int(raw_addr, f"Modbus point '{point.point_id}': address")
    if addr < 0:
        raise ConfigError(f"Modbus point '{point.point_id}': address must be >= 0, got {addr}")

    explicit_count = _extra(address, "count")
    if explicit_count is not None:
        count = _as_int(explicit_count, f"Modbus point '{point.point_id}': count")
        if count <= 0:
            raise ConfigError(f"Modbus point '{point.point_id}': count must be > 0, got {count}")
    else:
        count = _count_for_data_type(register_type, point.data_type)

    byte_order = str(_extra(address, "byte_order") or default_byte_order)
    if byte_order not in _VALID_BYTE_ORDERS:
        raise ConfigError(
            f"Modbus point '{point.point_id}': invalid byte_order '{byte_order}'; "
            f"must be one of {sorted(_VALID_BYTE_ORDERS)}"
        )

    return ModbusPoint(
        point_id=point.point_id,
        register_type=register_type,
        address=addr,
        count=count,
        data_type=point.data_type,
        byte_order=byte_order,
    )


def group_consecutive_reads(
    points: list[ModbusPoint],
    max_gap: int = 8,
    max_registers_per_request: int = 125,
) -> list[list[ModbusPoint]]:
    """Group points into runs that can be read by single Modbus requests.

    Points of different ``register_type`` cannot share a request (each maps to
    a distinct Modbus function code), so they are partitioned first.  Within a
    type, points are sorted by address and merged whenever the gap between
    consecutive points (in address units) is at most ``max_gap``.

    A group's total span — ``max(address + count) - min(address)``, i.e. the
    quantity the driver puts on the wire, including intra-group holes — must
    not exceed ``max_registers_per_request`` (125 is the Modbus protocol
    limit for register reads; step23: previously unbounded, so large
    contiguous point tables produced requests the server rejected).  Groups
    are closed at the last point that still fits, so the split always lands
    on a point boundary.
    """
    if not points:
        return []

    by_type: dict[str, list[ModbusPoint]] = {}
    for p in points:
        by_type.setdefault(p.register_type, []).append(p)

    groups: list[list[ModbusPoint]] = []
    for register_type in sorted(by_type):
        ordered = sorted(by_type[register_type], key=lambda p: p.address)
        current: list[ModbusPoint] = [ordered[0]]
        prev_end = ordered[0].address + ordered[0].count
        for p in ordered[1:]:
            span = max(prev_end, p.address + p.count) - current[0].address
            if p.address - prev_end <= max_gap and span <= max_registers_per_request:
                current.append(p)
            else:
                groups.append(current)
                current = [p]
            prev_end = max(prev_end, p.address + p.count)
        groups.append(current)

    return groups
