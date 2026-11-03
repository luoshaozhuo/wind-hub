"""Resolve opaque Secret references only through supported deployment mechanisms."""

from __future__ import annotations

import os
import re

from sink_service.domain.config import ConfigurationError


def resolve_secret(reference: str) -> str | None:
    """Read an env-backed secret without copying secret content into errors."""
    if not reference:
        return None
    if not reference.startswith("env:"):
        raise ConfigurationError("unsupported Secret backend; expected env: reference")
    key = reference[4:]
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) is None:
        raise ConfigurationError("invalid Secret environment reference")
    value = os.environ.get(key)
    if not value:
        raise ConfigurationError(f"missing Secret environment variable: {key}")
    return value
