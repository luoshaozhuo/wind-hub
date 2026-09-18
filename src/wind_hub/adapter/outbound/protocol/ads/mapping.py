"""ADS point mapping — parse point addresses and map data types.

An ADS point address is a :class:`~wind_hub.config.schema.PointAddress` whose
``extra`` fields carry::

    index_group:  symbol group (e.g. 0x4020)
    index_offset: byte offset within the group
    data_type:    (optional) ADS type name override (e.g. "REAL", "BOOL")
    size:         (optional) explicit byte size (e.g. for fixed-length strings)

When ``data_type`` is omitted, ``ADSPoint.data_type`` is derived from the
point's :data:`~wind_hub.config.schema.PointConfig.data_type` via
:func:`map_data_type`.
"""

from __future__ import annotations

from dataclasses import dataclass

from wind_hub.config.schema import PointConfig
from wind_hub.domain.model.errors import ConfigError

# wind-hub data_type (keys are the uppercased source values) → ADS type name.
_WINDHUB_TO_ADS: dict[str, str] = {
    "BOOL": "BOOL",
    "INT8": "SINT",
    "UINT8": "USINT",
    "INT16": "INT",
    "UINT16": "UINT",
    "INT32": "DINT",
    "UINT32": "UDINT",
    "FLOAT32": "REAL",
    "FLOAT64": "LREAL",
    "STR": "STRING",
    "STRING": "STRING",
}

# ADS type name → byte size.  ``0`` denotes variable length (strings).
_ADS_TYPE_SIZES: dict[str, int] = {
    "BOOL": 1,
    "SINT": 1,
    "USINT": 1,
    "INT": 2,
    "UINT": 2,
    "DINT": 4,
    "UDINT": 4,
    "REAL": 4,
    "LREAL": 8,
    "STRING": 0,
}


@dataclass(frozen=True)
class ADSPoint:
    """A single ADS point with its resolved address and type."""

    point_id: str
    index_group: int
    index_offset: int
    data_type: str
    """ADS type name: ``"BOOL"``/``"SINT"``/``"INT"``/``"REAL"``/``"STRING"`` …"""

    size: int
    """Byte size (``0`` = variable length, e.g. a null-terminated string)."""

    symbol: str | None = None
    """PLC symbol name for symbol addressing (``read_list_by_name`` and device
    notifications).  ``None`` means the point is addressed by
    ``index_group``/``index_offset`` only."""


def map_data_type(data_type: str) -> tuple[int, str]:
    """Map a wind-hub ``data_type`` to ``(size_bytes, ads_type_name)``.

    A string has no fixed size, so its size is reported as ``0``.
    """
    ads_name = _WINDHUB_TO_ADS.get(data_type.upper())
    if ads_name is None:
        raise ConfigError(f"ADS: unsupported data_type '{data_type}'")
    return (_ADS_TYPE_SIZES[ads_name], ads_name)


def _normalize_ads_type(raw: object) -> str:
    """Normalise an ADS type name, accepting wind-hub names as aliases."""
    name = str(raw).strip().upper()
    if name in _WINDHUB_TO_ADS:
        name = _WINDHUB_TO_ADS[name]
    if name not in _ADS_TYPE_SIZES:
        raise ConfigError(
            f"ADS: unsupported data type '{raw}'; " f"supported: {sorted(_ADS_TYPE_SIZES)}"
        )
    return name


def parse_point(point: PointConfig) -> ADSPoint:
    """Resolve a :class:`~wind_hub.config.schema.PointConfig` to an :class:`ADSPoint`.

    The ADS type may be given explicitly via the address ``data_type`` extra
    field (or the ``type`` field); otherwise it is mapped from the point's
    ``data_type``.  An explicit ``size`` overrides the type's default size.

    Raises:
        ConfigError: On a missing ``index_group``/``index_offset``, or an
            unsupported data type.
    """
    address = point.address
    extra = address.model_extra or {}

    symbol = extra.get("symbol")
    symbol = str(symbol) if symbol is not None else None

    index_group = extra.get("index_group")
    index_offset = extra.get("index_offset")
    if symbol is None:
        # Symbol addressing is optional; index/offset addressing requires both.
        if index_group is None or index_offset is None:
            raise ConfigError(
                f"ADS point '{point.point_id}': missing 'symbol' or "
                f"'index_group'/'index_offset' in address"
            )
    else:
        # Symbol addressing defaults index_group/index_offset to 0 (unused).
        index_group = index_group if index_group is not None else 0
        index_offset = index_offset if index_offset is not None else 0

    explicit_type = extra.get("data_type") if extra.get("data_type") is not None else address.type
    if explicit_type is not None:
        ads_name = _normalize_ads_type(explicit_type)
        size = _ADS_TYPE_SIZES[ads_name]
    else:
        size, ads_name = map_data_type(point.data_type)

    explicit_size = extra.get("size")
    if explicit_size is not None:
        size = int(explicit_size)
        if size < 0:
            raise ConfigError(f"ADS point '{point.point_id}': size must be >= 0, got {size}")

    return ADSPoint(
        point_id=point.point_id,
        index_group=int(index_group),
        index_offset=int(index_offset),
        data_type=ads_name,
        size=size,
        symbol=symbol,
    )
