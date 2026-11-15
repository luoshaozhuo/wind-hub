"""Infrastructure YAML interpretation of Collector service-source selection."""

from __future__ import annotations

import yaml


def sources_changed(before: bytes, after: bytes) -> bool:
    """Treat a changed source tree as restart-only, never hot-load it."""
    previous = yaml.safe_load(before)
    proposed = yaml.safe_load(after)
    if not isinstance(previous, dict) or not isinstance(proposed, dict):
        raise ValueError("Collector service configuration must be a mapping")
    return previous.get("config_sources") != proposed.get("config_sources")
