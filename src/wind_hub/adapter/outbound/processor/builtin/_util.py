"""Shared helpers for the builtin processors."""

from __future__ import annotations

from typing import Any


def is_numeric(value: Any) -> bool:
    """Return ``True`` when *value* is an ``int``/``float`` but not a ``bool``.

    ``bool`` is a subclass of ``int`` in Python, so it must be excluded
    explicitly; boolean points are never subject to numeric transforms
    (unit conversion, deadband filtering, or range checking).
    """
    return isinstance(value, int | float) and not isinstance(value, bool)
