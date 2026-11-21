"""Data Generator static Service restart policy and immutable Scenario ownership."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def requires_service_restart(
    previous: Mapping[str, Any], proposed: Mapping[str, Any]
) -> bool:
    """Require restart whenever the selected Service changes."""
    return previous != proposed
